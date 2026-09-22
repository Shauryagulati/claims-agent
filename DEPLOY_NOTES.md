# Deployment notes

Engineering register for the ECS Fargate deployment, 21 and 22 September 2026.
Kept because the failure modes below are all cheap to prevent and expensive to
diagnose from the symptom.

Nothing in the deployment hard-failed. Two of the three main entries are
near-misses caught before `apply`; the third is a design constraint that
shaped the file layout. That is the honest framing: the value here is in the
reasoning, not in a war story.

## 1. ARM64 against a default x86 task definition

The build machine is Apple Silicon, so `docker build` emits `linux/arm64`
without being asked. Fargate's default `runtimePlatform` is `X86_64`. Pushing
a native local build to a default task definition produces:

    exec /usr/local/bin/uvicorn: exec format error

The task then dies, ECS restarts it, and the service never stabilises. Two
things make this expensive. The message appears in the application's own log
stream, under the application's log group, at the moment the application was
supposed to start, so it reads as a broken entrypoint or a bad dependency. And
the deployment circuit breaker rolls back, which removes the evidence if you
are not tailing logs at the time.

Resolved by declaring the platform explicitly on both sides rather than
relying on either default:

- `runtime_platform { cpu_architecture = "ARM64" }` in the task definition.
- `docker build --platform linux/arm64` on the build, followed by
  `docker image inspect --format '{{.Architecture}}/{{.Os}}'` as a gate before
  the push. That check is the one step that turns a confusing runtime failure
  into an obvious local one.

ARM64 was chosen over cross-building to x86 for two reasons: Fargate ARM64 is
about 20 per cent cheaper per vCPU-hour, and the image that runs in the cloud
is then bit-for-bit the image that was tested locally, with no emulation in
either direction.

## 2. Three ECR entries per push, and whether the lifecycle rule was dangerous

The first push of a single image produced three entries in
`aws ecr describe-images`: the tag, plus two untagged entries of 63.7 MB and
1.4 KB. Buildx had pushed an OCI image index rather than a plain manifest. The
tagged entry is the index; the 63.7 MB untagged entry is the actual arm64
image manifest, and the 1.4 KB one is a provenance attestation.

That raised a real question, because the lifecycle policy expires untagged
images after one day, and the image the task actually runs was sitting in the
repository untagged. If that rule could delete it, the deployment would break
roughly 24 hours after a successful push, which is the worst possible timing
for diagnosis.

The AWS lifecycle policy documentation answers it directly. Two rules from the
evaluation logic:

> If an image is referenced by a manifest list, it cannot be expired or
> archived without the manifest list being deleted or archived first.

> When reference artifacts are present in a repository, Amazon ECR lifecycle
> policies automatically expire or archive those artifacts within 24 hours of
> the deletion or archival of the subject image.

So the arm64 manifest is protected while the tagged index references it, and
the attestation's lifetime is tied to its subject rather than evaluated
independently. The rule was never dangerous.

A second problem survived that answer. The retention rule is
`tagStatus: any, imageCountMoreThan: 5`, and it counts repository entries, not
deployments. At three entries per push, "keep the last five" means something
closer to the last one and a half deployments, except the protected children
cannot actually be expired, so the effective retention is neither five nor
predictable. The fix is at the build, not in the policy:

    docker build --provenance=false --sbom=false

One manifest per push, so the count means what it reads as. Verified with
`aws ecr get-lifecycle-policy-preview`, which evaluates the policy on demand
instead of waiting for the 24-hour cycle.

Worth noting the existing multi-manifest image was deliberately left in place
rather than replaced. It works, and the documented protection means it will
keep working; replacing it would have meant deleting images for tidiness
alone. ECR tag immutability means a replacement would also have required
deleting the tag first, since the same tag cannot be pushed twice.

## 3. Why the API key is not in Terraform

Terraform state is a plaintext JSON record of every attribute of every
resource it manages. An `aws_secretsmanager_secret_version` with the key in
`secret_string` puts that key in `terraform.tfstate` in cleartext. So does a
`data "aws_secretsmanager_secret_version"` lookup, because reading an
attribute writes it to state. Neither is meaningfully better than committing
the key to a `.tf` file; both are worse, because the exposure is not where
anyone looks for it.

The split:

- Terraform owns `aws_secretsmanager_secret`, the container. Name, description,
  recovery window. No value.
- The value is written once, out of band, with
  `aws secretsmanager put-secret-value`.
- The task definition references the secret by ARN in its `secrets` block. The
  ECS agent resolves it at container start using the execution role and
  injects it as an environment variable. The value never appears in the task
  definition, the ECS console, or state.

No `lifecycle { ignore_changes }` guard is needed, and one was deliberately
not added. Terraform has no concept of the value, so there is nothing for a
later apply to clobber. A guard there would have implied a relationship that
does not exist.

Verification is a one-liner worth keeping in the habit:

    grep -c 'sk-ant' infra/terraform.tfstate

Zero. The execution role's read permission is scoped to that single secret
ARN, and the ARN carries a random six-character suffix, so it is referenced
through `aws_secretsmanager_secret.anthropic_api_key.arn` rather than
reconstructed from the name.

## Smaller findings

**`.dockerignore` patterns are not recursive.** `__pycache__/` and `*.pyc`
match only at the context root. `app/__pycache__` with sixteen `.pyc` files
was therefore being copied into the image by `COPY app/ app/`, despite
`PYTHONDONTWRITEBYTECODE=1` being set specifically so none would exist.
Corrected to `**/__pycache__` and `**/*.pyc`. Low severity, but it means host
artifacts were shipping in the image.

**`python:3.11-slim` contains neither curl nor wget.** The reflexive ECS
container health check, `CMD-SHELL curl -f http://localhost:8000/health`,
would therefore fail on every evaluation and ECS would kill every task as
unhealthy. This deployment has no container health check at all, by choice:
the application calls `require_api_key()` inside the FastAPI lifespan hook
before the server accepts connections, so a missing key exits the process with
code 3 and `essential = true` stops the task. If a health check is ever wanted,
it has to be `python -c` with `urllib.request`, not curl.

Note the corollary, since this deployment has no load balancer either: with no
target group health check and no container health check, a process that hangs
without exiting would not be detected. Fail-fast covers the startup case, not
a wedged runtime.

**`COPY pytest.ini .`** was shipping a pytest configuration into an image that
contains no tests. Removed.

**A transient DNS failure mid-apply.** One `terraform apply` aborted with
`dial tcp: lookup logs.us-east-1.amazonaws.com: no such host` during the
refresh phase. Nothing had been created at that point, resolution succeeded on
retry, and the apply completed normally. Recorded only so that a single
unexplained resolution error during refresh is not mistaken for a
misconfiguration.

## The endpoint problem, unresolved by design

There is no load balancer, so the endpoint is the public IP attached to the
task's network interface. That address changes every time the task is
replaced, which includes every deployment and every park-and-unpark cycle.
There is no DNS name and no stable address, and the sequence to find the
current one is three AWS CLI calls chained through the ENI ID.

This is accepted rather than solved. An ALB fixes it and brings a stable
hostname and TLS, at roughly 16 US dollars a month against a total running
cost of about 11. For a deployment that is brought up to demonstrate something
and then parked, the arithmetic does not favour it.
