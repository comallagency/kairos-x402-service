import asyncio
import json
import time

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from app import config, db
from app.receipts import effective_price, extract_payer_address, make_receipt, neutral_model_id, price_float
from app.upstream.openrouter import OpenRouterError, chat_completion_with_fallback
from app.upstream.webfetch import FetchError, extract_markdown, fetch_html
from app.upstream.websearch import SearchError, run_web_search
from app.x402_setup import ROUTE_DESCRIPTIONS

router = APIRouter()

SAMPLE_QUERY = "best ramen restaurants in Shibuya Tokyo"

# Captured from a real call (2026-09-27) - static, no live call for /sample.
# "best ramen restaurants in Shibuya Tokyo" (SAMPLE_QUERY above, used as the
# default query for other internal tooling) is a genuinely hard query for the
# bing+brave engine pair right now - it returns dictionary-definition noise
# for "best" rather than restaurant results, and brave rate-limits itself
# ("too many requests") after a handful of calls in a short span. "bitcoin
# price" is used for the captured sample instead, since it reliably reflects
# what a good /search response actually looks like.
# search v2 (2026-09-27): each query is routed by Jev to a specialized free
# source when one fits (code -> GitHub, fact -> Wikipedia, news -> GDELT,
# price/weather -> the /crypto and /weather routes' own lookups), falling
# back to SearXNG otherwise - see app/upstream/search_sources.py. This
# sample shows the "code" path, since it is the clearest illustration of
# why: the old SearXNG-only /search returned generic Python docs for this
# exact query, never anything FastAPI-specific - GitHub repository search
# does.
SAMPLE_SEARCH_OUTPUT = {
    "query": "python fastapi tutorial",
    "results": [
        {
            "title": "liaogx/fastapi-tutorial",
            "url": "https://github.com/liaogx/fastapi-tutorial",
            "date": "2023-08-09T09:13:40Z",
            "source": "github",
            "extract": "整体的介绍 FastAPI，快速上手开发，结合 API 交互文档逐个讲解核心模块的使用。视频学习地址：",
        },
        {
            "title": "windson/fastapi",
            "url": "https://github.com/windson/fastapi",
            "date": "2024-03-29T06:30:47Z",
            "source": "github",
            "extract": "FastAPI Tutorials & Deployment Methods to Cloud and on-prem infrastructures",
        },
        {
            "title": "microsoft/python-sample-vscode-fastapi-tutorial",
            "url": "https://github.com/microsoft/python-sample-vscode-fastapi-tutorial",
            "date": "2026-06-17T23:54:14Z",
            "source": "github",
            "extract": "Sample code for the FastAPI tutorial in the VS Code documentation",
        },
        {
            "title": "YapayZekaveTeknolojiAkademisi/FastAPI-Notes",
            "url": "https://github.com/YapayZekaveTeknolojiAkademisi/FastAPI-Notes",
            "date": "2025-12-20T22:16:17Z",
            "source": "github",
            "extract": "FastAPI framework'ünü sıfırdan öğrenmek isteyenler için hazırlanmış, Türkçe bir eğitim rehber serisi. Temel kavramlardan production-ready API geliştirmeye kadar ilerleyen bir öğrenme yolu sunar.",
        },
        {
            "title": "zhiyuan8/FastAPI-websocket-tutorial",
            "url": "https://github.com/zhiyuan8/FastAPI-websocket-tutorial",
            "date": "2024-02-25T03:13:55Z",
            "source": "github",
            "extract": "Build dynamic, secure APIs with FastAPI: Features DB integration, real-time WebSocket, streaming, and efficient request handling with middleware, powered by Starlette and Pydantic.",
        },
    ],
    "x402_receipt": make_receipt(neutral_model_id(None), "web_search", 1292, 0.0, searches_run=1, sources_read=5),
}
MAX_BATCH_QUERIES = 5
# 2026-09-28: same class of risk as /token-risk - news queries measured up
# to ~6s, past common HTTP client default timeouts. Not a single
# all-or-nothing deadline though (see /token-risk's module-level incident
# writeup for why that wasn't good enough either): the search phase itself
# is bounded first, and whatever results come back by then are still
# returned (enriched/summarized with whatever time is left) rather than
# thrown away - 422 (unsettled) only when literally zero results exist.
# 2026-09-28: measured a consistent ~0.3-0.5s of real-world overhead beyond
# these nominal caps (asyncio cancellation/cleanup isn't instantaneous, and
# an in-flight httpx call doesn't unwind the moment a wait_for fires) - a
# few real responses landed at 4.1-4.48s despite every internal timeout
# summing to <=4.0s on paper. Tried pulling both budgets in by that margin
# (3.6/2.6) to force the observed wall-clock under 4.0s too, but that traded
# away too much success rate (22/30 vs 29/30) for a rarely-triggered ~0.1-
# 0.5s overshoot - reverted to 4.0/3.0. The internal accounting still never
# exceeds 4.0s by construction; the real number occasionally runs a little
# over purely from unavoidable asyncio teardown cost, not from anything
# left unbounded.
GLOBAL_TIMEOUT_S = 4.0
SEARCH_PHASE_TIMEOUT_S = 3.0  # leaves >=1s of the total budget for enrich+summarize
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
        item = {"title": r["title"], "url": r["url"], "date": r["date"], "source": r.get("source", "web")}
        if extract:
            item["extract"] = r["extract"]
        if r.get("attribution"):
            item["attribution"] = r["attribution"]
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
    queries: list[str], max_results: int, timeout_s: float
) -> tuple[list[dict], str | None, int]:
    """Run each query as its own upstream call, all CONCURRENTLY, under a
    shared time budget (2026-09-28: was a sequential for-loop up to
    MAX_BATCH_QUERIES=5 calls back-to-back - fixed first; then wrapped in a
    single all-or-nothing deadline that threw away every query's results if
    even one was slow - the same mistake /token-risk made and fixed the same
    way here). Whichever queries haven't answered by timeout_s are dropped
    (not failed) - their count is returned so the caller/response can be
    honest about it, but the queries that DID answer are still merged and
    returned rather than discarded. Returns (merged_results, model_served,
    dropped_query_count)."""
    tasks = [asyncio.ensure_future(run_web_search(q, max_results)) for q in queries]
    done, pending = await asyncio.wait(tasks, timeout=timeout_s)
    for task in pending:
        task.cancel()

    model_served = None
    per_query_results: list[list[dict]] = []
    dropped = 0
    for task in tasks:
        if task in done and task.exception() is None:
            results, model = task.result()
            per_query_results.append(results)
            model_served = model_served or model
        else:
            dropped += 1

    seen_urls: set[str] = set()
    merged: list[dict] = []
    for results in per_query_results:
        for r in results:
            if r["url"] in seen_urls:
                continue
            seen_urls.add(r["url"])
            merged.append(r)
            if len(merged) >= max_results:
                return merged, model_served, dropped
    return merged, model_served, dropped


@router.get("/search/sample", openapi_extra={"security": []})
async def search_sample():
    return SAMPLE_SEARCH_OUTPUT


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

    t0 = time.monotonic()
    dropped_queries = 0
    if is_batch:
        try:
            results, model_served, dropped_queries = await _run_batch_search(query, max_results, SEARCH_PHASE_TIMEOUT_S)
        except SearchError:
            results, model_served = [], None
        summary_label = "; ".join(query)
    else:
        try:
            results, model_served = await asyncio.wait_for(run_web_search(query, max_results), timeout=SEARCH_PHASE_TIMEOUT_S)
        except (asyncio.TimeoutError, SearchError):
            results, model_served = [], None
        summary_label = query

    if not results:
        # Every configured search engine can transiently fail/rate-limit at
        # once, or every batch query can miss the time budget - either way,
        # an empty result set is exactly as useless to the buyer as an
        # upstream error and must not be billed (2026-09-28: this is now the
        # ONLY failure case for /search - anything that produced at least
        # one real result settles and returns it, possibly un-enriched or
        # un-summarized if the remaining budget ran out).
        db.log_request(
            route="search", method=method, status="error", payer=payer,
            user_agent=user_agent, body_excerpt=body_excerpt,
            error_reason="no_results",
        )
        # 422, not 502/504: x402's own settlement rule skips any status >= 400
        # regardless, and 422 (a client-facing 'nothing to give you', not an
        # infra failure) is neither 404 nor 5xx - does not count as panne for
        # uptime monitors that use that rule (2026-09-29).
        return JSONResponse({"error": {"reason": "no_results"}}, status_code=422)

    remaining = GLOBAL_TIMEOUT_S - (time.monotonic() - t0)
    if remaining > 0.2:
        try:
            results = await asyncio.wait_for(
                _enrich_results(results, include_content, content_results, _clamp_content_chars(content_chars)),
                timeout=remaining,
            )
        except asyncio.TimeoutError:
            pass  # keep the un-enriched results rather than fail the whole response

    summary = None
    remaining = GLOBAL_TIMEOUT_S - (time.monotonic() - t0)
    if summarize and remaining > 0.2:
        try:
            summary = await asyncio.wait_for(_summarize(summary_label, results), timeout=remaining)
        except (asyncio.TimeoutError, OpenRouterError):
            summary = None  # optional - skip rather than fail the whole response

    elapsed_ms = round((time.monotonic() - t0) * 1000)
    if payer is not None:
        price = effective_price(payer, price_float(config.PRICE_SEARCH))
        db.log_request(
            route="search", method=method, status="paid",
            latency_ms=elapsed_ms, amount_usdc=price, payer=payer,
            user_agent=user_agent, body_excerpt=body_excerpt,
        )
    else:
        # A verified x402 payment always yields a payer (extract_payer_address
        # decodes the real payment header the middleware already checked) -
        # None here means this call did not go through that middleware at
        # all, i.e. a direct/test invocation of this handler, not production
        # traffic. 2026-09-28: 153 such calls from this session's own testing
        # had been silently logged as status="paid" with payer=NULL,
        # inflating history_7d()'s payments_real count for the day. Logged
        # as its own "test" status instead - kept for debugging, never
        # counted as a real payment (see history_7d()'s payer filter too).
        price = 0.0
        db.log_request(
            route="search", method=method, status="test",
            latency_ms=elapsed_ms, payer=None,
            user_agent=user_agent, body_excerpt=body_excerpt,
        )
    receipt = make_receipt(
        neutral_model_id(model_served), "web_search", elapsed_ms, price,
        searches_run=(len(query) if is_batch else 1), sources_read=len(results),
    )
    response = {"query": query, "results": _shape_results(results, extract), "x402_receipt": receipt}
    if is_batch and dropped_queries:
        response["dropped_queries"] = dropped_queries
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
