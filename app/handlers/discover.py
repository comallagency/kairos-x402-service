import math

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app import db
from app.upstream.ollama import OllamaError, embed
from app.upstream.websearch import SearchError, run_web_search
from app.x402_setup import KIT_TAGS

router = APIRouter()

# Pas de registre MCP local a interroger (aucun des registres publics n'expose
# une API de recherche unique et stable) : cette route passe par SearXNG, qui
# indexe deja github.com, les registres MCP connus et les annonces de serveurs,
# puis reclasse les resultats par similarite semantique avec nomic-embed-text
# (tourne sur ce VPS, sans cle ni quota - voir app/upstream/ollama.py). C'est
# un reclassement, pas un crawl exhaustif des 9400 serveurs recenses : la
# fraicheur et la couverture dependent de ce que SearXNG a indexe.

MAX_RESULTS_CAP = 10
CANDIDATE_POOL = 15
SAMPLE_QUERY = "extract text from a PDF"

DESCRIPTION = (
    "Discover MCP servers matching a need, ranked by semantic relevance - "
    "free, no account, no payment. Searches indexed MCP registries and "
    "listings, then reranks with a local embedding model. Not an exhaustive "
    "crawl of every registered server."
)


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


async def _rank_by_relevance(query: str, results: list[dict], max_results: int) -> list[dict]:
    if not results:
        return []
    texts = [query] + [f"{r['title'] or ''} {r['extract'] or ''}".strip() for r in results]
    try:
        vectors = await embed(texts)
    except OllamaError:
        # Le classement SearXNG brut reste utilisable si l'embedding echoue -
        # degrader plutot que renvoyer une erreur sur une route gratuite.
        return results[:max_results]
    query_vec, result_vecs = vectors[0], vectors[1:]
    scored = sorted(
        zip(results, result_vecs), key=lambda pair: _cosine(query_vec, pair[1]), reverse=True
    )
    ranked = []
    for r, vec in scored[:max_results]:
        ranked.append({**r, "relevance": round(_cosine(query_vec, vec), 4)})
    return ranked


def _shape(results: list[dict]) -> list[dict]:
    return [
        {"title": r["title"], "url": r["url"], "extract": r.get("extract"), "relevance": r.get("relevance")}
        for r in results
    ]


async def _run_discover(q: str, max_results: int) -> tuple[list[dict], str | None]:
    search_query = f"MCP server \"model context protocol\" {q}"
    results, _ = await run_web_search(search_query, max_results=CANDIDATE_POOL)
    ranked = await _rank_by_relevance(q, results, max_results)
    return ranked, None


@router.get("/discover/sample", openapi_extra={"security": []}, tags=KIT_TAGS + ["discovery", "free"])
async def discover_sample():
    try:
        ranked, _ = await _run_discover(SAMPLE_QUERY, 5)
    except SearchError as exc:
        return JSONResponse(
            {"error": {"reason": "upstream_error", "detail": str(exc)[:200]}}, status_code=502
        )
    return {"q": SAMPLE_QUERY, "results": _shape(ranked)}


@router.get(
    "/discover",
    openapi_extra={"security": []},
    summary="Find MCP servers by need, ranked by semantic relevance - free.",
    description=DESCRIPTION,
    tags=KIT_TAGS + ["discovery", "free"],
)
async def discover(request: Request, q: str, max_results: int = 5):
    user_agent = request.headers.get("user-agent")
    if not q or not q.strip():
        db.log_request(
            route="discover", method="GET", status="error",
            user_agent=user_agent, error_reason="missing_q",
        )
        return JSONResponse({"error": {"reason": "missing_q"}}, status_code=400)

    max_results = max(1, min(max_results, MAX_RESULTS_CAP))
    try:
        ranked, _ = await _run_discover(q, max_results)
    except SearchError as exc:
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
    return {"q": q, "results": _shape(ranked)}
