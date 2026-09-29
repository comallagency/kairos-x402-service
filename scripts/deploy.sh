#!/usr/bin/env bash
# Deploy x402-app: blue-green behind nginx, zero-downtime, then a MANDATORY
# smoke test.
#
# Run from the repo root (/opt/x402/app):
#   bash scripts/deploy.sh
#
# Replaces the old `docker compose up -d --no-deps --force-recreate x402`,
# which tore down the live container before the replacement was confirmed
# ready - a real 35-60s window where nginx's proxied backend was unreachable
# (measured via a 45-events-in-one-minute 502 burst in nginx's x402 access
# log, 2026-09-28). Now: build the new image, start it in whichever of the
# two compose services (x402_blue / x402_green) is NOT currently receiving
# traffic, wait for its own Docker healthcheck, only THEN flip nginx's
# proxy_pass target (a `set $x402_backend ...;` snippet + `nginx -s reload`,
# which drains in-flight connections instead of dropping them) - and only
# stop the old container after the smoke test confirms the new one is good
# live, through nginx, for real. If the new container never turns healthy,
# or the post-cutover smoke test fails, nginx is left on (or rolled back to)
# whichever backend was last known good - the old container is never
# stopped until a replacement has proven itself, so a failed deploy attempt
# never causes an outage on its own.
#
# The smoke test (scripts/smoke_test.py) isn't a separate step to remember
# afterward - it's the last thing this script does, and a failure here makes
# the script itself exit non-zero, so "the deploy failed" is a fact the exit
# code reports on its own, not something that depends on a human (or an
# agent's memory) choosing to check.
#
# Added 2026-09-28 after a DynamicPrice bug crashed /.well-known/x402 with
# a 500 for several deploys before anyone noticed - every one of those
# deploys was "verified" only by curling whatever route had just changed,
# which never included the one that broke. See CLAUDE.md.

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

echo "== cutting nginx over to $target_service (port $target_port) =="
echo "set \$x402_backend 127.0.0.1:$target_port;" > "$UPSTREAM_SNIPPET"
if ! nginx -t; then
    echo "DEPLOY FAILED: nginx config test failed after rewriting $UPSTREAM_SNIPPET - reverting." >&2
    echo "set \$x402_backend 127.0.0.1:$live_port;" > "$UPSTREAM_SNIPPET"
    exit 1
fi
systemctl reload nginx
echo "nginx now proxying to $target_service."

# GET /admin/data.json is a real DB-aggregation endpoint (not a static
# file), and its first call against a just-started container is slow
# enough (measured ~5s, cold caches/connections) that the /admin/live
# headless render check's own request timed out - visible in nginx's
# access log as the check's fetch getting a 499 (client closed the
# connection) twice in a row when smoke_test.py fired immediately after
# cutover, 2026-09-29. Confirmed nginx itself was never at fault (every
# other real request in both failed windows got a normal 200/402; direct-
# to-container access was never affected either) - this is a cold-start
# latency on our own aggregation query, not an nginx reload artifact. The
# sleep gives that first real hit somewhere to land before it's also the
# one deciding whether this deploy is good. Bumped 2s->3s the same day
# GET /admin/data.json grew a second expensive computation (the 24h
# funnel/agents/per-route aggregation moved server-side, see
# app/admin.py::_compute_agg_24h) - same root cause, same fix, just more
# margin needed since there's more real work on that first hit now.
sleep 3

echo "== running post-cutover smoke test (scripts/smoke_test.py) against $target_service, live through nginx =="
if ! docker exec "$target_container" python3 scripts/smoke_test.py; then
    echo "DEPLOY FAILED: smoke test did not pass after cutover - rolling nginx back to $live_service (port $live_port)." >&2
    echo "set \$x402_backend 127.0.0.1:$live_port;" > "$UPSTREAM_SNIPPET"
    nginx -t && systemctl reload nginx
    echo "Rolled back. $live_service is untouched and still running - investigate $target_service before retrying." >&2
    exit 1
fi
echo "smoke test passed on $target_service."

echo "== stopping old container ($live_service, port $live_port) =="
docker compose stop "$live_service"
docker compose rm -f "$live_service"

echo "== deploy OK: $target_service is live (port $target_port), $live_service stopped, smoke test passed =="
