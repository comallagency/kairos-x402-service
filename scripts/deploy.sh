#!/usr/bin/env bash
# Deploy x402-app: blue-green behind nginx, zero-downtime, with a MANDATORY
# two-stage safety net: a full pre-cutover check against the new container
# BEFORE nginx ever sees it, and a monitored post-cutover window AFTER.
#
# Run from the repo root (/opt/x402/app):
#   bash scripts/deploy.sh
#
# History: the original single-container `docker compose up -d --no-deps
# --force-recreate x402` tore down the live container before the
# replacement was confirmed ready (2026-09-28, a 45-events-in-one-minute
# 502 burst). Blue-green (x402_blue/x402_green) fixed that. Then a single
# post-cutover smoke test (2026-09-28/29) still meant a genuinely broken
# build could reach real traffic for the ~5-10s between cutover and that
# one check running - fine for the bugs it happened to catch, no guarantee
# against the ones it didn't test.
#
# Rebuilt 2026-09-30 into two explicit stages:
#
#   1. PRE-CUTOVER (scripts/precutover_check.py): the full smoke test PLUS
#      a real GET .../sample call for every route in the live catalog,
#      run against the new container's own internal port - nginx is not
#      touched yet, so a single failure here means zero external impact by
#      construction, not by luck. Only on a clean pass does nginx flip.
#
#   2. POST-CUTOVER (scripts/post_cutover_watch.py): the same smoke test,
#      repeated, against the real public domain through nginx, for 60
#      seconds. The OLD container is kept running (not stopped) through
#      this entire window - on the first failure, nginx rolls back to it
#      immediately and the new container is torn down. Only after 60
#      clean seconds does the old container finally get stopped.
#
# A failed deploy at either stage makes this script exit non-zero - "the
# deploy failed" is a fact the exit code reports on its own.

set -euo pipefail

UPSTREAM_SNIPPET="/etc/nginx/snippets/x402_upstream.conf"
BLUE_PORT=18402
GREEN_PORT=18403
HEALTH_TIMEOUT_TRIES=30   # 30 * 2s = 60s
HEALTH_POLL_INTERVAL_S=2

echo "== docker compose build (shared image, both slots) =="
docker compose build x402_blue x402_green

current_port=$(grep -oE '127\.0\.0\.1:[0-9]+' "$UPSTREAM_SNIPPET" | grep -oE '[0-9]+$' || echo "")
if [ "$current_port" = "$GREEN_PORT" ]; then
    live_service="x402_green"; live_port=$GREEN_PORT
    target_service="x402_blue"; target_port=$BLUE_PORT
else
    # Default/first-run assumption: blue (18402) is live, matching the
    # pre-blue-green single-container setup this replaces.
    live_service="x402_blue"; live_port=$BLUE_PORT
    target_service="x402_green"; target_port=$GREEN_PORT
fi
echo "== currently live: $live_service (port $live_port) -> deploying into $target_service (port $target_port) =="

echo "== starting $target_service alongside the live container (no traffic yet) =="
docker compose up -d --no-deps "$target_service"

echo "== waiting for $target_service to report healthy =="
target_container=$(docker compose ps -q "$target_service")
status="unknown"
for _ in $(seq 1 "$HEALTH_TIMEOUT_TRIES"); do
    status=$(docker inspect --format='{{.State.Health.Status}}' "$target_container" 2>/dev/null || echo "unknown")
    if [ "$status" = "healthy" ]; then
        break
    fi
    sleep "$HEALTH_POLL_INTERVAL_S"
done

if [ "$status" != "healthy" ]; then
    echo "DEPLOY FAILED: $target_service did not become healthy (status: $status)." >&2
    echo "Live traffic is untouched - still on $live_service (port $live_port)." >&2
    docker logs "$target_container" --tail 50 >&2
    docker compose stop "$target_service" >&2 || true
    exit 1
fi
echo "$target_service is healthy."

echo "== pre-cutover check: full smoke test + every catalog route's /sample, against $target_service's own port (nginx untouched so far) =="
if ! docker exec "$target_container" python3 scripts/precutover_check.py http://127.0.0.1:8000; then
    echo "DEPLOY FAILED: pre-cutover check failed on $target_service - nginx was never touched, $live_service is still serving every request." >&2
    docker logs "$target_container" --tail 50 >&2
    docker compose stop "$target_service" >&2 || true
    docker compose rm -f "$target_service" >&2 || true
    exit 1
fi
echo "pre-cutover check passed on $target_service."

echo "== cutting nginx over to $target_service (port $target_port) =="
echo "set \$x402_backend 127.0.0.1:$target_port;" > "$UPSTREAM_SNIPPET"
if ! nginx -t; then
    echo "DEPLOY FAILED: nginx config test failed after rewriting $UPSTREAM_SNIPPET - reverting." >&2
    echo "set \$x402_backend 127.0.0.1:$live_port;" > "$UPSTREAM_SNIPPET"
    exit 1
fi
systemctl reload nginx
echo "nginx now proxying to $target_service."

# No blind sleep here (removed 2026-09-30): GET /admin/data.json's cold
# first call after a just-started container is genuinely slow (measured
# ~5s) and used to need a few seconds' grace before the first check - but
# a live drill the same day (killing the new container the instant cutover
# happened) proved that an unconditional sleep here is a real blind spot:
# it is unmonitored time, so a genuine post-cutover failure occurring
# during it produced real external 502s for the full length of the sleep
# with zero chance of being caught early. scripts/post_cutover_watch.py's
# own first check now fires immediately and gives exactly one grace retry
# to a failing pass (see its module docstring) - same tolerance for a real
# cold start, without ever going dark.
echo "== post-cutover watch: full smoke test against the public domain, every ${POLL_INTERVAL_S:-2}s for 60s (one grace retry on a failing pass), through $target_service =="
if ! docker exec "$target_container" python3 scripts/post_cutover_watch.py; then
    echo "DEPLOY FAILED: post-cutover watch failed - rolling nginx back to $live_service (port $live_port) now." >&2
    echo "set \$x402_backend 127.0.0.1:$live_port;" > "$UPSTREAM_SNIPPET"
    nginx -t && systemctl reload nginx
    echo "Rolled back. $live_service is untouched and still running. Stopping the bad $target_service." >&2
    docker logs "$target_container" --tail 50 >&2
    docker compose stop "$target_service" >&2 || true
    docker compose rm -f "$target_service" >&2 || true
    echo "Investigate $target_service before retrying." >&2
    exit 1
fi
echo "post-cutover watch passed clean on $target_service."

echo "== stopping old container ($live_service, port $live_port) =="
docker compose stop "$live_service"
docker compose rm -f "$live_service"

echo "== deploy OK: $target_service is live (port $target_port), $live_service stopped, pre-cutover + 60s post-cutover watch both passed =="
