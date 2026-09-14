import json
from datetime import datetime, timedelta, timezone

from app import config, db
from app.generated.dynamic_routes import dynamic_daily_capacity
from app.x402_setup import build_route_configs

# Every hand-built route (search/translate/jobs/pdf/web-read/extract/
# summarize/fact-check) plus anything the usine (see usine/) has added to
# app/generated/routes_registry.yaml - build_route_configs() already merges
# both, so this is never a second hand-typed list of "what routes exist"
# (that duplication is exactly what let /search's old price linger in one
# place after being changed in another, 2026-09-06). Computed once at import
# time, so a registry change only takes effect after the process restarts,
# matching how every other route-registration in this app already behaves.
ROUTE_KEYS = {
    tuple(route_key.split(" ", 1)): route_key.split(" ", 1)[1].lstrip("/")
    for route_key in build_route_configs()
}


def _next_utc_midnight() -> str:
    now = datetime.now(timezone.utc)
    tomorrow = (now + timedelta(days=1)).date()
    return datetime(tomorrow.year, tomorrow.month, tomorrow.day, tzinfo=timezone.utc).isoformat()


class CapacityGateMiddleware:
    """Pure-ASGI middleware, wrapped OUTSIDE the x402 payment middleware.

    Rejects requests with a plain 402 (no x402 payment challenge) before any
    payment is requested and before any upstream call, whenever our own daily
    counter is exhausted. There is no separate upstream quota to pre-check: the
    single upstream is OpenRouter, and an empty balance fails closed on its own
    (surfaced as a 502 upstream_error) - no surprise invoice is possible.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope["method"]
        path = scope["path"]
        route_key = ROUTE_KEYS.get((method, path))
        if route_key is None:
            await self.app(scope, receive, send)
            return

        block_reason = None
        reopens_at = None

        limit = config.DAILY_CAPACITY.get(route_key, dynamic_daily_capacity().get(route_key))
        if limit is not None and db.count_paid_today(route_key) >= limit:
            block_reason = "daily_capacity_reached"
            reopens_at = _next_utc_midnight()

        if block_reason is not None:
            headers = dict(scope.get("headers") or [])
            user_agent = headers.get(b"user-agent", b"").decode("latin-1")
            db.log_request(
                route=route_key,
                method=method,
                status="capacity_reached",
                user_agent=user_agent,
                error_reason=block_reason,
            )
            body = json.dumps(
                {"x402Version": 1, "error": {"reason": block_reason, "reopens_at": reopens_at}}
            ).encode("utf-8")
            await send(
                {
                    "type": "http.response.start",
                    "status": 402,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return

        await self.app(scope, receive, send)
