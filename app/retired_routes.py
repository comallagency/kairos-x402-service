"""Permanent retirement registry (CLAUDE.md rule, 2026-10-01): a route that
was ever publicly listed (Bazaar, PayAI, .well-known/x402, llms.txt) must
never just disappear. Disabling it at the catalog layer alone (the
GEMINI_FLASH_ENABLED/DEEPSEEK_ENABLED pattern, 2026-09-30) leaves the real
handler reachable - confirmed live on 2026-10-01: both routes executed a
real paid LLM call for a stranger with zero payment required, because the
x402 payment middleware only enforces paths present in its own RouteConfig
dict, and an unlisted path falls straight through to the FastAPI handler.

Entries here are enforced by RetiredRouteMiddleware, mounted as the
outermost layer in main.py (before payment/capacity/circuit-breaker
middleware and before the handler), so a retired path can never execute
and never depends on remembering to also strip it from every discovery
surface - it always answers 410 Gone with Sunset + a successor Link,
never 404, regardless of HTTP method.
"""
import json

HAND_RETIRED_ROUTES = {
    "/llm/gemini-flash": {
        "sunset": "Wed, 30 Sep 2026 00:00:00 GMT",
        "successor": "/llm/gpt-mini",
        "reason": "Provider p95 ~20s even with automatic fallback (10 real calls, 2026-09-30) - withdrawn pending a reliable provider/model.",
    },
    "/llm/deepseek": {
        "sunset": "Wed, 30 Sep 2026 00:00:00 GMT",
        "successor": "/llm/gpt-mini",
        "reason": "Provider p95 ~20s even with automatic fallback (10 real calls, 2026-09-30) - withdrawn pending a reliable provider/model.",
    },
}


def _generated_retired_entries() -> dict:
    """Usine routes Fossoyeur retires (app/generated/routes_registry.yaml,
    status: retired) must answer 410 exactly like a hand-built one - without
    this, a retired generated route simply stops being registered at all
    (app/generated/dynamic_routes.py only builds routers for live_routes())
    and falls through to a bare Starlette 404, the same "looks like an
    outage, not a decision" problem this whole mechanism exists to prevent.
    Read fresh at import time (process restarts on every Fossoyeur
    rebuild+redeploy, so no hot-reload is needed)."""
    try:
        from app.generated.registry import retired_routes as _registry_retired
    except ImportError:
        return {}
    entries = {}
    for spec in _registry_retired():
        retired_at = spec.retired_at or spec.born_at
        try:
            from datetime import datetime
            sunset = datetime.fromisoformat(retired_at).strftime("%a, %d %b %Y %H:%M:%S GMT")
        except ValueError:
            sunset = "Thu, 01 Jan 1970 00:00:00 GMT"
        entries[f"/{spec.slug}"] = {
            "sunset": sunset,
            "successor": "/discover",
            "reason": f"Retired by Fossoyeur: {spec.intention} (zero paid calls, 30+ days live).",
        }
    return entries


RETIRED_ROUTES = {**HAND_RETIRED_ROUTES, **_generated_retired_entries()}


def match_retired(path: str):
    for prefix, info in RETIRED_ROUTES.items():
        if path == prefix or path.startswith(prefix + "/"):
            return prefix, info
    return None, None


class RetiredRouteMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        prefix, info = match_retired(scope["path"])
        if info is None:
            await self.app(scope, receive, send)
            return
        body = json.dumps({
            "error": {
                "reason": "route_retired",
                "detail": info["reason"],
                "retired_path": prefix,
                "successor": info["successor"],
            }
        }).encode()
        headers = [
            (b"content-type", b"application/json"),
            (b"sunset", info["sunset"].encode()),
            (b"link", f'<{info["successor"]}>; rel="successor-version"'.encode()),
        ]
        await send({"type": "http.response.start", "status": 410, "headers": headers})
        await send({"type": "http.response.body", "body": body})
