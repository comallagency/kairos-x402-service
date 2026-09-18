from fastapi import APIRouter, Request
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

DESCRIPTION = (
    "Discover MCP servers matching a need, ranked by semantic similarity over "
    "a curated snapshot of observed MCP servers - free, no account, no payment. "
    "Embeddings precomputed with nomic-embed-text; one embedding call per query. "
    "Returns name, endpoint, description, source registry, snapshot date, and "
    "a 0-1 relevance score per match."
)


@router.get("/discover/sample", openapi_extra={"security": []}, tags=KIT_TAGS + ["discovery", "free"])
async def discover_sample():
    try:
        return await _run_snapshot_discover(SAMPLE_QUERY, DEFAULT_MAX_RESULTS, MIN_SIMILARITY)
    except OllamaError as exc:
        return JSONResponse(
            {"error": {"reason": "upstream_error", "detail": str(exc)[:200]}}, status_code=502
        )


@router.get(
    "/discover",
    openapi_extra={"security": []},
    summary="Find MCP servers by need, ranked by semantic relevance - free.",
    description=DESCRIPTION,
    tags=KIT_TAGS + ["discovery", "free"],
)
async def discover(request: Request, q: str, max_results: int = DEFAULT_MAX_RESULTS):
    user_agent = request.headers.get("user-agent")
    if not q or not q.strip():
        db.log_request(
            route="discover", method="GET", status="error",
            user_agent=user_agent, error_reason="missing_q",
        )
        return JSONResponse({"error": {"reason": "missing_q"}}, status_code=400)

    max_results = max(1, min(max_results, MAX_RESULTS_CAP))
    try:
        result = await _run_snapshot_discover(q.strip()[:500], max_results, MIN_SIMILARITY)
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
        user_agent=user_agent, body_excerpt=q[:2048],
    )
    return result
