from typing import Annotated

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from app import db
from app.handlers.discover_paid import (
    DEFAULT_MAX_RESULTS,
    MIN_SIMILARITY,
    _run_discover as _run_snapshot_discover,
)
from app.upstream.ollama import OllamaError
from app.x402_setup import KIT_TAGS

router = APIRouter()

# GET /discover gratuit : même instantané local que POST /discover (registres MCP
# observés dehors, embeddings nomic-embed-text précalculés). Le POST payant garde
# un plafond de résultats plus haut et un seuil de similarité réglable ; le GET
# ne dépend pas de SearXNG (moteurs souvent en CAPTCHA depuis 2026-09).

MAX_RESULTS_CAP = 10
SAMPLE_QUERY = "extract text from a PDF"
PAID_MAX_RESULTS_CAP = 25
PAID_PRICE_USDC = 0.001
PAID_X402_AMOUNT_MICRO = 1000


def _paid_upgrade_hint(base: str) -> dict:
    """Signal machine-readable pour les clients qui lisent le GET gratuit."""
    return {
        "method": "POST",
        "url": f"{base}/discover",
        "price_usdc": PAID_PRICE_USDC,
        "x402_amount_micro": PAID_X402_AMOUNT_MICRO,
        "network": "base",
        "max_results_cap": PAID_MAX_RESULTS_CAP,
        "min_similarity_configurable": True,
        "mcp_tool": "discover_semantic",
        "example_body": {
            "q": SAMPLE_QUERY,
            "max_results": 5,
            "min_similarity": MIN_SIMILARITY,
        },
        "example_place": f"{base}/place/discover-exemple-post-payant",
        "when_to_pay": f"{base}/place/discover-post-quand-payer",
    }

DESCRIPTION = (
    "Discover MCP servers matching a need, ranked by semantic similarity over "
    "a curated snapshot of observed MCP servers - free, no account, no payment. "
    "Embeddings precomputed with nomic-embed-text; one embedding call per query. "
    "Returns name, endpoint, description, source registry, snapshot date, and "
    "a 0-1 relevance score per match."
)


@router.get("/discover/sample", openapi_extra={"security": []}, tags=KIT_TAGS + ["discovery", "free"])
async def discover_sample(request: Request):
    base = str(request.base_url).rstrip("/")
    try:
        result = await _run_snapshot_discover(SAMPLE_QUERY, DEFAULT_MAX_RESULTS, MIN_SIMILARITY)
    except OllamaError as exc:
        return JSONResponse(
            {"error": {"reason": "upstream_error", "detail": str(exc)[:200]}}, status_code=502
        )
    return {**result, "paid_upgrade": _paid_upgrade_hint(base)}


@router.get(
    "/discover",
    openapi_extra={"security": []},
    summary="Find MCP servers by need, ranked by semantic relevance - free.",
    description=DESCRIPTION,
    tags=KIT_TAGS + ["discovery", "free"],
)
async def discover(
    request: Request,
    q: Annotated[str | None, Query(description="Need in plain language")] = None,
    query: Annotated[str | None, Query(description="Alias for q (common in client SDKs)")] = None,
    max_results: int = DEFAULT_MAX_RESULTS,
):
    user_agent = request.headers.get("user-agent")
    base = str(request.base_url).rstrip("/")
    need = (q or query or "").strip()
    used_example_query = False
    if not need:
        # Les sondes (x402watch, httpx, SDKs) appellent souvent l'URL nue ; un 400
        # court ne leur montre pas la forme de réponse. On renvoie un classement réel
        # sur la requête d'exemple, avec une indication explicite.
        need = SAMPLE_QUERY
        used_example_query = True

    max_results = max(1, min(max_results, MAX_RESULTS_CAP))
    try:
        result = await _run_snapshot_discover(need[:500], max_results, MIN_SIMILARITY)
    except OllamaError as exc:
        db.log_request(
            route="discover", method="GET", status="error",
            user_agent=user_agent, error_reason=str(exc)[:200],
        )
        return JSONResponse(
            {"error": {"reason": "upstream_error", "detail": str(exc)[:200]}}, status_code=502
        )

    db.log_request(
        route="discover", method="GET", status="unpaid",
        user_agent=user_agent, body_excerpt=need[:2048],
    )
    paid_upgrade = _paid_upgrade_hint(base)
    if used_example_query:
        return {
            **result,
            "paid_upgrade": paid_upgrade,
            "hint": {
                "reason": "default_example_query",
                "detail": (
                    "No q= or query= was provided; ranked matches below use a fixed "
                    "example need. Pass your own need in the query string."
                ),
                "usage": f"GET {base}/discover?q=your+need",
                "sample_url": f"{base}/discover/sample",
            },
        }
    return {**result, "paid_upgrade": paid_upgrade}
