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

RETIRED_ROUTES = {
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
