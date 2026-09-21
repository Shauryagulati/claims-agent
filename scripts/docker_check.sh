#!/usr/bin/env bash
# Build an image from a directory, run it with the key from the environment,
# create a session, send the Nadia message, and require a real reply with
# phase PROCESS_CASE. One live turn. The key is never printed.
#
#   scripts/docker_check.sh [context_dir] [image_tag] [host_port]
set -euo pipefail

CTX="${1:-.}"
TAG="${2:-claims-agent:check}"
PORT="${3:-8010}"
NAME="claims-agent-check-$$"

if [ -z "${ANTHROPIC_API_KEY:-}" ] && [ -f .env ]; then
  set -a; . ./.env; set +a
fi
if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
  echo "ANTHROPIC_API_KEY is not set and no .env found" >&2
  exit 2
fi

echo "== build from ${CTX}"
docker build -q -t "${TAG}" "${CTX}" >/dev/null
echo "== image built: ${TAG}"

echo "== image must not contain .env"
if docker run --rm --entrypoint sh "${TAG}" -c 'test -e /srv/.env' 2>/dev/null; then
  echo "FAIL: .env is inside the image" >&2; exit 1
fi
echo "   ok"

cleanup() { docker rm -f "${NAME}" >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "== run"
docker run -d --rm --name "${NAME}" -p "${PORT}:8000" -e ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY}" "${TAG}" >/dev/null

echo "== wait for /health"
for i in $(seq 1 30); do
  if curl -fs "http://localhost:${PORT}/health" >/dev/null 2>&1; then break; fi
  sleep 1
  if [ "$i" = 30 ]; then echo "FAIL: /health never answered" >&2; docker logs "${NAME}" >&2; exit 1; fi
done
curl -s "http://localhost:${PORT}/health"; echo

echo "== create session"
SID=$(curl -fs -X POST "http://localhost:${PORT}/api/session" | python3 -c 'import sys,json; print(json.load(sys.stdin)["session_id"])')
echo "   session ${SID}"

echo "== send the Nadia message (one live turn)"
MSG='I am the policyholder. My name is Nadia Okonkwo, policy POL-3318. I am calling about my denied healthcare claim from July. DOB is 1987-06-09, SSN last four is 2907.'
RESP=$(curl -fs -X POST "http://localhost:${PORT}/api/session/${SID}/message" \
  -H 'Content-Type: application/json' \
  --data "$(python3 -c 'import json,sys; print(json.dumps({"text": sys.argv[1]}))' "${MSG}")")

python3 - "${RESP}" <<'PY'
import json, sys
r = json.loads(sys.argv[1])
reply, phase = r.get("reply", ""), r.get("phase")
print("   phase:", phase)
print("   reply:", reply[:300].replace("\n", " ") + ("..." if len(reply) > 300 else ""))
apology = "something went wrong on my side" in reply.lower()
if phase != "PROCESS_CASE" or not reply.strip() or apology:
    print("FAIL: expected a real reply in PROCESS_CASE", file=sys.stderr)
    sys.exit(1)
PY

echo "== state"
curl -fs "http://localhost:${PORT}/api/session/${SID}/state" | python3 -c '
import sys, json
s = json.load(sys.stdin)
print("   verified:", s["memory"]["verified_party_id"], " resolved:", s["memory"]["resolved_case_id"])
print("   directives:", [d["kind"] for d in s["last_directives"]])
assert s["memory"]["verified_party_id"] == "PH-4021" and s["memory"]["resolved_case_id"] == "CLM-7710"
'
echo "PASS: container built, started, verified, resolved, and answered"
