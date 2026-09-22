# infra

Terraform for running the claims agent on ECS Fargate. One task, ARM64, in a
public subnet with its own public IP. No load balancer, no NAT Gateway.

Everything here can be destroyed and rebuilt from scratch. This file is the
order to do it in.

## Prerequisites

- Terraform 1.16 or newer.
- AWS CLI configured with credentials that can create ECR, ECS, IAM, VPC,
  CloudWatch Logs and Secrets Manager resources. Default region us-east-1,
  or set `aws_region` yourself.
- Docker with buildx, able to produce a `linux/arm64` image. On an Apple
  Silicon machine that is the native output. On an x86 machine it is an
  emulated cross-build and considerably slower.
- An Anthropic API key.

## What it costs

Running continuously, roughly 11 US dollars a month:

| Item | Per month |
|---|---|
| Fargate ARM64, 0.25 vCPU and 0.5 GB | 7.21 |
| One public IPv4 address | 3.65 |
| Secrets Manager, one secret | 0.40 |
| ECR storage, five images retained | 0.07 |
| CloudWatch Logs, seven-day retention | under 0.10 |

Parked at `desired_count = 0` the Fargate and IPv4 charges stop and the rest
comes to about 0.50 a month. Nothing here is in the AWS always-free tier.

## Bring it up

The image cannot be pushed before the registry exists, and the service should
not start before the image and the secret value do. The way through that is
to create everything with the service scaled to zero, fill in the two things
Terraform does not own, then scale up.

### 1. Variables

    cp terraform.tfvars.example terraform.tfvars

Edit it:

- `allowed_cidr` is the only address allowed to reach port 8000. The
  application has no authentication and every answered message spends
  Anthropic credit, so this is never `0.0.0.0/0`. Your current address:

      curl -s https://checkip.amazonaws.com

  written as `a.b.c.d/32`.

- `image_tag` is the git SHA you are about to build:

      git rev-parse --short HEAD

- `desired_count` is `0` for the first apply.

### 2. Create the infrastructure with nothing running

    terraform init
    terraform apply

Expect 18 resources. The task definition registers even though the image it
names does not exist yet; ECS does not check the registry until it starts a
task, and with `desired_count = 0` it never does.

### 3. Put the API key in Secrets Manager

Terraform creates the secret container and deliberately never the version, so
that the key is not in state. Write the value yourself:

    read -rs -p "Anthropic key: " KEY && echo
    aws secretsmanager put-secret-value \
      --secret-id "$(terraform output -raw anthropic_secret_arn)" \
      --secret-string "$KEY"
    unset KEY

Confirm it took, without printing it:

    aws secretsmanager list-secret-version-ids \
      --secret-id "$(terraform output -raw anthropic_secret_arn)" \
      --query 'length(Versions)'

One version is what you want. An empty secret makes the task fail at start
with an error that reads like a permissions problem.

### 4. Build and push the image

From the repository root, not this directory:

    export REPO=$(terraform -chdir=infra output -raw ecr_repository_url)
    export SHA=$(git rev-parse --short HEAD)
    export ACCOUNT=$(aws sts get-caller-identity --query Account --output text)

    aws ecr get-login-password --region us-east-1 \
      | docker login --username AWS --password-stdin \
          "$ACCOUNT.dkr.ecr.us-east-1.amazonaws.com"

    docker build --platform linux/arm64 --provenance=false --sbom=false \
      -t "$REPO:$SHA" .

    docker image inspect "$REPO:$SHA" --format '{{.Architecture}}/{{.Os}}'

That must print `arm64/linux`. If it prints `amd64`, stop: the task
definition declares ARM64 and the task will crash-loop on `exec format
error`, which does not look like an architecture problem in the logs.

    docker push "$REPO:$SHA"

`--provenance=false --sbom=false` keeps the push to a single manifest. Without
them, buildx pushes an image index plus an attestation manifest and ECR lists
three entries per push, which makes the lifecycle policy's retention count
mean something other than what it says.

### 5. Start the task

Set `desired_count = 1` in `terraform.tfvars`, then:

    terraform apply

## Find the endpoint

There is no load balancer and no DNS name. The endpoint is the public IP on
the task's network interface, and it changes every time the task is replaced.

    T=$(aws ecs list-tasks --cluster claims-agent --service-name claims-agent \
          --query 'taskArns[0]' --output text)
    E=$(aws ecs describe-tasks --cluster claims-agent --tasks "$T" \
          --query "tasks[0].attachments[0].details[?name=='networkInterfaceId'].value | [0]" \
          --output text)
    aws ec2 describe-network-interfaces --network-interface-ids "$E" \
      --query 'NetworkInterfaces[0].Association.PublicIp' --output text

If `list-tasks` returns `None`, the task has not started yet. Watch it:

    aws ecs describe-services --cluster claims-agent --services claims-agent \
      --query 'services[0].{desired:desiredCount,running:runningCount}'

## Confirm it works

    IP=<the address from above>

    curl -s "http://$IP:8000/health"

A JSON body with `"ok":true` proves four things at once: the image ran on
ARM64, the secret resolved, the internet gateway route works, and the
security group admits you. The server only accepts connections after the key
check passes, so a refused connection means the container exited.

One live turn, which spends Anthropic credit:

    SID=$(curl -s -X POST "http://$IP:8000/api/session" | jq -r .session_id)

    curl -s -X POST "http://$IP:8000/api/session/$SID/message" \
      -H 'Content-Type: application/json' --data-binary @- <<'JSON' | jq .
    {"text": "I'm the policyholder. My name is Nadia Okonkwo, policy POL-3318. I'm calling about my denied healthcare claim from July. DOB is 1987-06-09, SSN last four is 2907."}
    JSON

    curl -s "http://$IP:8000/api/session/$SID/state" \
      | jq '{verified: .memory.verified_party_id, resolved: .memory.resolved_case_id}'

Phase `PROCESS_CASE`, `PH-4021` and `CLM-7710`.

The browser UI is at `http://$IP:8000` and includes the debug panel.

## Logs

There is no container health check, so this is where startup failures show up.

    aws logs tail /ecs/claims-agent --follow

## Park it

    desired_count = 0     # in terraform.tfvars
    terraform apply

Do not use `aws ecs update-service --desired-count 0`. It works, and it makes
the state file a lie: the next unrelated `terraform apply` sets the count
back to 1 and restarts the meter.

Unparking gives the task a new public IP.

## Deploy new code

    git commit ...
    SHA=$(git rev-parse --short HEAD)
    docker build --platform linux/arm64 --provenance=false --sbom=false -t "$REPO:$SHA" .
    docker push "$REPO:$SHA"
    # set image_tag to the new SHA in terraform.tfvars
    terraform apply

That registers a new task definition revision and the service replaces the
task. Rolling back is setting `image_tag` to the previous SHA and applying
again. With one task and no load balancer there is a gap of a minute or so,
and the new task has a different public IP.

ECR tags are immutable, so the same SHA cannot be pushed twice. To replace an
image at a tag, delete it first:

    aws ecr batch-delete-image --repository-name claims-agent \
      --image-ids imageTag=$SHA

## Tear it down

    terraform destroy

Expect 18 resources destroyed. It takes a few minutes: the service drains and
the network interface detaches before the subnet and security group can go.

Two things are set up to make this clean. `force_delete` on the ECR
repository means the images go with it, and `recovery_window_in_days = 0` on
the secret deletes it immediately rather than scheduling deletion 30 days out
and blocking reuse of the name.

Because the images are deleted with the repository, rebuilding later means
running the sequence above from step 1 again, including the push. Your API
key is unaffected; Terraform never had it.

## Variables

| Name | Default | Notes |
|---|---|---|
| `aws_region` | `us-east-1` | |
| `project_name` | `claims-agent` | Name prefix and `Project` tag on everything |
| `allowed_cidr` | none | Required. The only source allowed on port 8000 |
| `image_tag` | none | Required. Git SHA of the image in ECR to run |
| `desired_count` | `1` | `0` parks the deployment |

## Files

| File | Contents |
|---|---|
| `versions.tf` | Terraform and provider version constraints |
| `providers.tf` | Region and default tags |
| `variables.tf` | Inputs |
| `ecr.tf` | Registry and lifecycle policy |
| `secrets.tf` | Secret container, no version |
| `logs.tf` | CloudWatch log group |
| `iam.tf` | Task execution role and its two policies |
| `network.tf` | VPC, public subnet, internet gateway, route table |
| `security_group.tf` | Security group and its two rules |
| `ecs.tf` | Cluster and task definition |
| `ecs_service.tf` | Service |
| `outputs.tf` | Registry URL and secret ARN |

`terraform.tfvars` and all state files are gitignored. `.terraform.lock.hcl`
is committed on purpose, so that `terraform init` resolves the same provider
build every time.
