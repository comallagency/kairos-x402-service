#!/usr/bin/env bash
# Resolves whichever of x402-app / x402-app-green is actually running right
# now - blue-green deploys (scripts/deploy.sh) alternate which name that is,
# and cron lines that hardcode either one silently stop working every other
# deploy. Found 2026-09-29: chain_payments.py's (*/30) and
# attract_agents.py's (3:17) crontab lines both had exactly this bug -
# `docker exec x402-app ...` with a literal, unchanging name.
#
# Usage: scripts/run_in_live_container.sh <command...>
# Exits 1 with no exec attempted if no x402-app* container is running -
# never silently no-ops.
set -euo pipefail

container=$(docker ps --filter "name=^x402-app" --format "{{.Names}}" | head -n1)
if [ -z "$container" ]; then
    echo "run_in_live_container.sh: no running x402-app* container found" >&2
    exit 1
fi
exec docker exec "$container" "$@"
