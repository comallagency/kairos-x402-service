import asyncio
import json

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from app import config, db
from app.receipts import Timer, effective_price, extract_payer_address, make_receipt, neutral_model_id, price_float
from app.upstream.openrouter import OpenRouterError, chat_completion_with_fallback
from app.upstream.webfetch import FetchError, extract_markdown, fetch_html
from app.upstream.websearch import SearchError, run_web_search
from app.x402_setup import ROUTE_DESCRIPTIONS

router = APIRouter()

SAMPLE_QUERY = "best ramen restaurants in Shibuya Tokyo"
MAX_BATCH_QUERIES = 5
MAX_CONTENT_RESULTS = 3
DEFAULT_CONTENT_CHARS = 12_000

SUMMARY_PROMPT = (
    "Summarize the following web search results in 2-4 sentences, neutral and "
    "factual, in direct response to the query. Do not invent facts beyond what is "
    "given in the extracts."
)


async def _summarize(query: str, results: list[dict]) -> str | None:
    if not results:
        return None
    context = "\n\n".join(
        f"[{r['title'] or r['url']}]({r['url']}): "
        f"{(r.get('content_markdown') or r.get('extract') or '')[:2000]}"
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


def _clamp_content_chars(value) -> int:
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = DEFAULT_CONTENT_CHARS
    return max(1_000, min(value, 20_000))


async def _enrich_result_content(result: dict, max_chars: int) -> dict:
    enriched = dict(result)
    try:
        html = await fetch_html(result["url"])
        markdown = extract_markdown(html, result["url"])
        if not markdown:
            enriched["content_error"] = "no_extractable_content"
        else:
            enriched["content_markdown"] = markdown[:max_chars]
            enriched["content_truncated"] = len(markdown) > max_chars
    except FetchError as exc:
        # One blocked or dead result must not make the whole paid search fail.
        enriched["content_error"] = str(exc)[:200]
    return enriched


async def _enrich_results(
    results: list[dict], include_content: bool, content_results: int, content_chars: int
) -> list[dict]:
    if not include_content or not results:
        return results
    try:
        requested = int(content_results)
    except (TypeError, ValueError):
        requested = MAX_CONTENT_RESULTS
    count = max(1, min(requested, MAX_CONTENT_RESULTS, len(results)))
    enriched = await asyncio.gather(
        *(_enrich_result_content(result, content_chars) for result in results[:count])
    )
    return [*enriched, *results[count:]]


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
    include_content: bool,
    content_results,
    content_chars,
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
            results = await _enrich_results(
                results,
                include_content,
                content_results,
                _clamp_content_chars(content_chars),
            )
            summary = await _summarize(summary_label, results) if summarize else None
    except (OpenRouterError, SearchError) as exc:
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
        include_content=bool(body.get("include_content", True)),
        content_results=body.get("content_results", 3),
        content_chars=body.get("content_chars", DEFAULT_CONTENT_CHARS),
        summarize=bool(body.get("summarize", False)),
        body_excerpt=body_excerpt,
    )
