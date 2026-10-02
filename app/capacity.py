import json
from datetime import datetime, timedelta, timezone

from app import config, db
from app.upstream.openrouter import has_sufficient_balance
from app.generated.dynamic_routes import dynamic_daily_capacity
from app.x402_setup import _build_route_configs_uncached

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
    for route_key in _build_route_configs_uncached()
}


def refresh_route_keys() -> None:
    """ROUTE_KEYS is computed once at import time, before purecalc/dynamic
    routes exist (see its own comment above) - mutated in place (not
    reassigned) so app.mpp_middleware's own `from app.capacity import
    ROUTE_KEYS` reference, bound once at ITS import time, sees the update
    too. Called from app_lifespan() once the app has fully imported, same
    pattern as x402_setup.invalidate_route_configs_cache()."""
    ROUTE_KEYS.clear()
    ROUTE_KEYS.update({
        tuple(route_key.split(" ", 1)): route_key.split(" ", 1)[1].lstrip("/")
        for route_key in _build_route_configs_uncached()
    })


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


class JobsCircuitBreakerMiddleware:
    """Pure-ASGI middleware, wrapped OUTSIDE the x402 payment middleware -
    same placement as CapacityGateMiddleware.

    Unlike search/translate/summarize/extract, which settle only after a
    successful OpenRouter call, POST /jobs settles payment at CREATION time
    (app/handlers/jobs.py::create_job) - the actual OpenRouter calls happen
    later, asynchronously, in app/jobs_worker.py::_run_job. A job created
    while the account can't afford its own worst case gets charged in full,
    then fails ("job_execution_error") with nothing delivered and no refund
    path.

    Checks the real (60s-cached) OpenRouter balance via
    app.upstream.openrouter.has_sufficient_balance before ever offering the
    x402 payment challenge. Below MIN_BALANCE_USD, returns a plain 503 - not
    a 402, so this reads as "temporarily unavailable" rather than "payment
    required for a service likely to fail regardless".
    """

    MIN_BALANCE_USD = 0.50

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST" or scope["path"] != "/jobs":
            await self.app(scope, receive, send)
            return

        if await has_sufficient_balance(self.MIN_BALANCE_USD):
            await self.app(scope, receive, send)
            return

        db.log_request(
            route="jobs",
            method="POST",
            status="circuit_breaker_open",
            error_reason="openrouter_balance_below_floor",
        )
        body = json.dumps(
            {
                "error": {
                    "reason": "temporarily_unavailable",
                    "detail": "Job creation is temporarily disabled (upstream credit low). Other routes are unaffected.",
                }
            }
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 503,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": body})


class LLMGatewayCircuitBreakerMiddleware:
    """Pure-ASGI middleware, wrapped OUTSIDE the x402 payment middleware -
    same placement and reasoning as JobsCircuitBreakerMiddleware.

    POST /v1/chat/completions uses the x402 "upto" scheme: the buyer
    authorizes a ceiling, and app/handlers/llm_gateway.py settles for the
    real OpenRouter cost afterward. If our own OpenRouter balance can't
    cover even one call, that settlement would still be attempted after an
    upstream failure the buyer already saw as a 502 - better to fail before
    ever quoting a price. Fixed floor rather than per-request (the ceiling
    itself is request-specific, computed in compute_ceiling_price(), but an
    ASGI middleware here would need to buffer and re-inject the body to peek
    at it, which every other capacity gate in this file also avoids)."""

    MIN_BALANCE_USD = 1.00

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST" or scope["path"] != "/v1/chat/completions":
            await self.app(scope, receive, send)
            return

        if await has_sufficient_balance(self.MIN_BALANCE_USD):
            await self.app(scope, receive, send)
            return

        db.log_request(
            route="v1/chat/completions",
            method="POST",
            status="circuit_breaker_open",
            error_reason="openrouter_balance_below_floor",
        )
        body = json.dumps(
            {
                "error": {
                    "reason": "temporarily_unavailable",
                    "detail": "The LLM gateway is temporarily disabled (upstream credit low). Other routes are unaffected.",
                }
            }
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 503,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": body})


class PinnedModelCircuitBreakerMiddleware:
    """Pure-ASGI middleware, wrapped OUTSIDE the x402 payment middleware -
    same placement and reasoning as LLMGatewayCircuitBreakerMiddleware,
    generalized to several paths at once for Pack 2's 5 pinned per-model
    LLM routes (2026-09-30). Each is "exact" scheme only (no settlement
    surprise the way "upto" has), but the same "don't quote a price for a
    call we can't afford" reasoning applies - a buyer paying the full
    ceiling for a call that then 502s on insufficient OpenRouter credit is
    still a bad outcome worth failing closed before ever offering the 402.
    Per-route floor since each model has a very different real per-token
    cost (Claude Sonnet vs Llama are not remotely the same worst case)."""

    FLOORS_USD = {
        "/llm/claude-sonnet": 1.00,
        "/llm/gpt-mini": 0.25,
        "/llm/gemini-flash": 0.25,
        "/llm/llama": 0.25,
        "/llm/deepseek": 0.25,
    }

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        floor = self.FLOORS_USD.get(scope["path"]) if scope["type"] == "http" and scope["method"] == "POST" else None
        if floor is None:
            await self.app(scope, receive, send)
            return

        if await has_sufficient_balance(floor):
            await self.app(scope, receive, send)
            return

        route = scope["path"].lstrip("/")
        db.log_request(
            route=route,
            method="POST",
            status="circuit_breaker_open",
            error_reason="openrouter_balance_below_floor",
        )
        body = json.dumps(
            {
                "error": {
                    "reason": "temporarily_unavailable",
                    "detail": "This model route is temporarily disabled (upstream credit low). Other routes are unaffected.",
                }
            }
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 503,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": body})
