#!/usr/bin/env bash
# Deploy x402-app: build, recreate, then a MANDATORY smoke test.
#
# Run from the repo root (/opt/x402/app):
#   bash scripts/deploy.sh
#
# This replaces running `docker compose build x402` and
# `docker compose up -d --no-deps --force-recreate x402` as two loose
# manual commands. The smoke test (scripts/smoke_test.py) isn't a separate
# step to remember afterward - it's the last thing this script does, and a
# failure here makes the script itself exit non-zero, so "the deploy
# failed" is a fact the exit code reports on its own, not something that
# depends on a human (or an agent's memory) choosing to check.
#
# Added 2026-09-28 after a DynamicPrice bug crashed /.well-known/x402 with
# a 500 for several deploys before anyone noticed - every one of those
# deploys was "verified" only by curling whatever route had just changed,
# which never included the one that broke. See CLAUDE.md.

set -euo pipefail

echo "== docker compose build x402 =="
docker compose build x402

echo "== docker compose up -d --no-deps --force-recreate x402 =="
docker compose up -d --no-deps --force-recreate x402

echo "== waiting for x402-app to report healthy =="
status="unknown"
for _ in $(seq 1 30); do
    status=$(docker inspect --format='{{.State.Health.Status}}' x402-app 2>/dev/null || echo "unknown")
    if [ "$status" = "healthy" ]; then
        break
    fi
    sleep 2
done

if [ "$status" != "healthy" ]; then
    echo "DEPLOY FAILED: x402-app did not become healthy (status: $status)" >&2
    docker logs x402-app --tail 50 >&2
    exit 1
fi
echo "x402-app is healthy."

echo "== running post-recreate smoke test (scripts/smoke_test.py) =="
if ! docker exec x402-app python3 scripts/smoke_test.py; then
    echo "DEPLOY FAILED: smoke test did not pass after recreate - the container is live and may be serving a broken route right now. Investigate before doing anything else." >&2
    exit 1
fi

echo "== deploy OK: x402-app healthy, smoke test passed =="
