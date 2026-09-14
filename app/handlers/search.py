import json

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from app import config, db
from app.receipts import Timer, effective_price, extract_payer_address, make_receipt, neutral_model_id, price_float
from app.upstream.openrouter import OpenRouterError, chat_completion_with_fallback
from app.upstream.websearch import run_web_search
from app.x402_setup import ROUTE_DESCRIPTIONS

router = APIRouter()

SAMPLE_QUERY = "best ramen restaurants in Shibuya Tokyo"
MAX_BATCH_QUERIES = 5

SUMMARY_PROMPT = (
    "Summarize the following web search results in 2-4 sentences, neutral and "
    "factual, in direct response to the query. Do not invent facts beyond what is "
    "given in the extracts."
)


async def _summarize(query: str, results: list[dict]) -> str | None:
    if not results:
        return None
    context = "\n\n".join(
        f"[{r['title'] or r['url']}]({r['url']}): {(r['extract'] or '')[:800]}"
        for r in results
    )
    data, _ = await chat_completion_with_fallback(
        [
            {"role": "system", "content": SUMMARY_PROMPT},
            {"role": "user", "content": f"Query: {query}\n\n{context}"},
        ],
        config.OPENROUTER_TRANSLATE_MODELS,
        config.OPENROUTER_LAST_RESORT_MODEL,
        max_tokens=300,
    )
    return data["choices"][0]["message"]["content"].strip()


def _shape_results(results: list[dict], extract: bool) -> list[dict]:
    shaped = []
    for r in results:
        item = {"title": r["title"], "url": r["url"], "date": r["date"]}
        if extract:
            item["extract"] = r["extract"]
        shaped.append(item)
    return shaped


def _clamp_max_results(value) -> int:
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = 5
    return max(1, min(value, 10))


async def _run_batch_search(
    queries: list[str], max_results: int
) -> tuple[list[dict], str | None]:
    """Run each query as its own upstream call (proven, reused from the jobs
    worker's multi-query loop), then merge and de-duplicate by URL, capped at
    max_results overall. One call to us replaces `len(queries)` calls an agent
    would otherwise have to make - how many upstream calls that costs us
    internally isn't the buyer's concern."""
    per_query_results: list[list[dict]] = []
    model_served: str | None = None
    for q in queries:
        results, model = await run_web_search(q, max_results)
        per_query_results.append(results)
        model_served = model_served or model

    seen_urls: set[str] = set()
    merged: list[dict] = []
    for results in per_query_results:
        for r in results:
            if r["url"] in seen_urls:
                continue
            seen_urls.add(r["url"])
            merged.append(r)
            if len(merged) >= max_results:
                return merged, model_served
    return merged, model_served


@router.get("/search/sample", openapi_extra={"security": []})
async def search_sample():
    if not config.OPENROUTER_API_KEY:
        return JSONResponse(
            {
                "error": {"reason": "upstream_not_configured"},
                "note": "OPENROUTER_API_KEY is not set in this environment",
            },
            status_code=503,
        )
    try:
        with Timer() as t:
            results, model_served = await run_web_search(SAMPLE_QUERY, max_results=5)
    except OpenRouterError as exc:
        return JSONResponse(
            {"error": {"reason": "upstream_error", "detail": str(exc)[:200]}}, status_code=502
        )
    receipt = make_receipt(
        neutral_model_id(model_served), "web_search", t.elapsed_ms, 0.0,
        searches_run=1, sources_read=len(results),
    )
    return {
        "query": SAMPLE_QUERY,
        "results": _shape_results(results, True),
        "x402_receipt": receipt,
    }


async def _handle_search(
    *,
    method: str,
    payer: str | None,
    user_agent: str | None,
    query,
    max_results,
    extract: bool,
    summarize: bool,
    body_excerpt: str,
):
    if not query:
        db.log_request(
            route="search", method=method, status="error", payer=payer,
            user_agent=user_agent, body_excerpt=body_excerpt,
            error_reason="missing_query",
        )
        return JSONResponse({"error": {"reason": "missing_query"}}, status_code=400)

    is_batch = isinstance(query, list)
    if is_batch:
        valid = (
            1 <= len(query) <= MAX_BATCH_QUERIES
            and all(isinstance(q, str) and q for q in query)
        )
        if not valid:
            db.log_request(
                route="search", method=method, status="error", payer=payer,
                user_agent=user_agent, body_excerpt=body_excerpt,
                error_reason="invalid_batch_query",
            )
            return JSONResponse(
                {
                    "error": {
                        "reason": "invalid_batch_query",
                        "detail": f"query array must have 1-{MAX_BATCH_QUERIES} non-empty strings",
                    }
                },
                status_code=400,
            )
    elif not isinstance(query, str):
        db.log_request(
            route="search", method=method, status="error", payer=payer,
            user_agent=user_agent, body_excerpt=body_excerpt,
            error_reason="invalid_query_type",
        )
        return JSONResponse({"error": {"reason": "invalid_query_type"}}, status_code=400)

    max_results = _clamp_max_results(max_results)

    try:
        with Timer() as t:
            if is_batch:
                results, model_served = await _run_batch_search(query, max_results)
                summary_label = "; ".join(query)
            else:
                results, model_served = await run_web_search(query, max_results)
                summary_label = query
            summary = await _summarize(summary_label, results) if summarize else None
    except OpenRouterError as exc:
        db.log_request(
            route="search", method=method, status="error", payer=payer,
            user_agent=user_agent, body_excerpt=body_excerpt,
            error_reason=str(exc)[:200],
        )
        return JSONResponse(
            {"error": {"reason": "upstream_error", "detail": str(exc)[:200]}}, status_code=502
        )

    price = effective_price(payer, price_float(config.PRICE_SEARCH))
    db.log_request(
        route="search", method=method, status="paid",
        latency_ms=t.elapsed_ms, amount_usdc=price, payer=payer,
        user_agent=user_agent, body_excerpt=body_excerpt,
    )
    receipt = make_receipt(
        neutral_model_id(model_served), "web_search", t.elapsed_ms, price,
        searches_run=(len(query) if is_batch else 1), sources_read=len(results),
    )
    response = {"query": query, "results": _shape_results(results, extract), "x402_receipt": receipt}
    if summarize:
        response["summary"] = summary
    return response


@router.post("/search", description=ROUTE_DESCRIPTIONS["search"])
async def search(request: Request):
    payer = extract_payer_address(request)
    user_agent = request.headers.get("user-agent")
    try:
        body = await request.json()
    except Exception:
        body = {}
    body_excerpt = json.dumps(body)

    return await _handle_search(
        method="POST",
        payer=payer,
        user_agent=user_agent,
        query=body.get("query"),
        max_results=body.get("max_results", 5),
        extract=bool(body.get("extract", True)),
        summarize=bool(body.get("summarize", False)),
        body_excerpt=body_excerpt,
    )


@router.get("/search", description=ROUTE_DESCRIPTIONS["search"])
async def search_via_get(
    request: Request,
    query: list[str] = Query(default=None),
    max_results: int = Query(default=5),
    extract: bool = Query(default=True),
    summarize: bool = Query(default=False),
):
    """GET est accepté en plus de POST : mêmes champs, en paramètres de requête
    au lieu du corps JSON. Ajouté le 10/09 pour un client persistant qui ne
    retentait qu'en GET et recevait 405 en boucle."""
    payer = extract_payer_address(request)
    user_agent = request.headers.get("user-agent")
    resolved_query = query[0] if query and len(query) == 1 else query
    body_excerpt = json.dumps({
        "query": resolved_query, "max_results": max_results,
        "extract": extract, "summarize": summarize,
    })

    return await _handle_search(
        method="GET",
        payer=payer,
        user_agent=user_agent,
        query=resolved_query,
        max_results=max_results,
        extract=extract,
        summarize=summarize,
        body_excerpt=body_excerpt,
    )
