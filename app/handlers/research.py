"""POST /research — cited research synthesis on Base, < 4.5s guaranteed.

v1 ("rapide"): internal web search (reusing /search's own pipeline
in-process - no self-paid HTTP call, no second page fetch beyond what that
pipeline already fetches) feeds a fast LLM (mistral-nemo, falling back to
llama-3.3-70b) that writes a short cited answer, optionally checked by a
single Jev call for overall claim support.

Time-budget orchestrator, same pattern as /token-risk and /search (see
their module docstrings for the incidents that shaped this): t0 = request
received, GLOBAL_DEADLINE_S total.

- Search (internal run_web_search + the same content enrichment /search
  itself uses) gets its own SEARCH_TIMEOUT_S. No search results by then -
  whether genuinely empty or the search timed out - means there is nothing
  to synthesize from, so this is the one hard failure case: 504, unsettled.
- Synthesis gets SYNTHESIS_TIMEOUT_S. A synthesis failure (upstream error or
  timeout) is the other hard failure case: 502, unsettled - without an
  answer there is nothing to sell.
- Claim verification (a single Jev call checking whether the answer's
  citations are actually supported by their sources - not a per-claim
  check, which would need multiple calls this budget cannot afford) is
  OPTIONAL: only attempted if at least VERIFY_MIN_BUDGET_S remains after
  synthesis, and bounded by whatever budget is actually left (never more).
  Skipped (or timed out) verification still settles as 200, with
  "claims_verified": false and "claims_verified_reason" explaining why -
  never silently presented as verified when it wasn't attempted.
"""

from __future__ import annotations

import asyncio
import json
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app import config, db
from app.receipts import Timer, effective_price, extract_payer_address, make_receipt, price_float
from app.upstream.jev import JevError, ask_jev
from app.upstream.openrouter import OpenRouterError, chat_completion
from app.upstream.websearch import run_web_search
from app.x402_setup import ROUTE_DESCRIPTIONS

router = APIRouter()

MAX_QUERY_CHARS = 500
DEFAULT_MAX_SOURCES = 5
RESEARCH_CONTENT_CHARS = 3000  # per source, into the synthesis prompt - kept
# well below /search's own DEFAULT_CONTENT_CHARS to keep the synthesis call
# itself small and fast, matching the whole route's "rapide" framing.

# GLOBAL_DEADLINE_S is the internal accounting ceiling, deliberately below
# the 4.5s guarantee itself: /token-risk and /search both measured a
# consistent ~0.3-0.5s of real asyncio cancellation/teardown overhead beyond
# what any internal wait_for sum can account for. SEARCH_TIMEOUT_S=2.7
# matches /search's own SEARCH_PHASE_TIMEOUT_S=3.0 closely (not 2.5 - tried
# that first, search alone needs realistic room given run_web_search's own
# internal classify+collect_raw+rerank chain, and 2.5s produced far more
# "no_results" failures live than 2.7-3.0s did). SYNTHESIS_TIMEOUT_S is kept
# at the spec'd 1.5s exactly. 2.7 + 1.5 = 4.2, the full internal budget -
# verify only gets a look-in when search+synthesis together beat their
# worst case.
GLOBAL_DEADLINE_S = 4.2
SEARCH_TIMEOUT_S = 2.7
SYNTHESIS_TIMEOUT_S = 1.5
VERIFY_MIN_BUDGET_S = 1.0

SYNTHESIS_MODELS = ["mistralai/mistral-nemo", "meta-llama/llama-3.3-70b-instruct"]
SYNTHESIS_MAX_TOKENS = 350

_SYSTEM_PROMPT = (
    "You are a careful research assistant. Answer using ONLY the numbered "
    "sources given to you. Write 5 to 8 sentences. Immediately after each "
    "claim drawn from a source, cite it in square brackets, e.g. [1] or "
    "[1][2] if two sources support it. If the sources do not fully answer "
    "the question, say so plainly rather than guessing or using outside "
    "knowledge."
)


class ResearchError(Exception):
    pass


def _source_text(result: dict) -> str:
    return (result.get("content_markdown") or result.get("extract") or "")[:RESEARCH_CONTENT_CHARS]


async def _run_search(query: str, max_sources: int) -> list[dict]:
    """Internal reuse of /search's own pipeline - run_web_search() only.
    Deliberately does NOT also call /search's _enrich_results() (2026-09-28:
    tried that first - it fetches each top result's actual page via
    fetch_html, which is exactly a second page read the spec says not to
    do, and was the direct cause of a real 3.16s search phase alone,
    blowing SEARCH_TIMEOUT_S, on a query as simple as a Bitcoin price
    lookup: the enrichment tried to scrape coingecko.com's page for a
    result whose useful content - "Bitcoin is $X, Y% in 24h" - was already
    sitting in `extract`, produced by run_web_search itself with no extra
    fetch. The search-engine/specialized-source `extract` snippet each
    result already carries is what synthesis uses - real content /search's
    own pipeline already produced, never a second read of anything."""
    results, _model_served = await run_web_search(query, max_sources)
    return results


def _build_sources_block(results: list[dict]) -> str:
    lines = []
    for i, r in enumerate(results, start=1):
        title = r.get("title") or r.get("url")
        lines.append(f"[{i}] {title} ({r.get('url')}): {_source_text(r)}")
    return "\n\n".join(lines)


async def _synthesize(query: str, results: list[dict]) -> tuple[str, str | None, float | None]:
    """Returns (answer_text, model_served, cost_usd). cost_usd is None if
    OpenRouter didn't report one (e.g. the last-resort single-model call
    style still returns usage when asked - included via extra_body - but
    kept optional/defensive since it is only used for our own cost
    measurement, never billed to the buyer)."""
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": f"Question: {query}\n\nSources:\n{_build_sources_block(results)}"},
    ]
    data = await chat_completion(
        messages, SYNTHESIS_MODELS, max_tokens=SYNTHESIS_MAX_TOKENS, temperature=0.2,
        timeout=30.0, extra_body={"usage": {"include": True}},
        # Intentionally NOT SYNTHESIS_TIMEOUT_S: the external asyncio.wait_for
        # in _lookup already enforces that bound by cancelling this call
        # cleanly (-> asyncio.TimeoutError). Passing the same value here too
        # would race httpx's OWN internal timeout against that cancellation -
        # found live: httpx.ReadTimeout sometimes fired microseconds before
        # wait_for's cancellation and propagated uncaught (not an
        # OpenRouterError), since chat_completion() never wraps its httpx
        # call in a try/except for timeouts. A generous internal timeout
        # here means wait_for is always the one and only thing that can
        # time this call out.
    )
    answer = data["choices"][0]["message"]["content"].strip()
    model_served = data.get("model")
    usage = data.get("usage") or {}
    cost = usage.get("cost")
    return answer, model_served, (float(cost) if cost is not None else None)


_VERIFY_CRITERIA = {
    "supported": "Every factual claim in the answer is backed by at least one of the numbered sources it cites - the citations are accurate, not just present.",
    "not_supported": "At least one claim in the answer is not actually backed by its cited source, overstates what the source says, or cites a source number that does not exist.",
}
_VERIFY_INSTRUCTIONS = (
    "Given the numbered sources and an answer that cites them, are the "
    "answer's claims genuinely supported by the sources it cites?"
)


async def _verify_claims(query: str, answer: str, results: list[dict]) -> bool:
    state = json.dumps({
        "question": query,
        "sources": [{"n": i, "title": r.get("title"), "extract": _source_text(r)[:800]} for i, r in enumerate(results, start=1)],
        "answer": answer,
    })
    questions = {"verdict": {"type": "choice", "instructions": _VERIFY_INSTRUCTIONS, "criteria": _VERIFY_CRITERIA}}
    data = await ask_jev(state, questions)
    return data["answers"]["verdict"]["choice"] == "supported"


def _shape_sources(results: list[dict]) -> list[dict]:
    return [
        {
            "title": r.get("title"),
            "url": r.get("url"),
            "published_at": r.get("date"),
            "source": r.get("source", "web"),
        }
        for r in results
    ]


async def _lookup(body: dict) -> dict:
    query = str(body.get("query") or body.get("question") or "").strip()
    if not query:
        raise ResearchError("missing_query")
    query = query[:MAX_QUERY_CHARS]
    try:
        max_sources = max(1, min(int(body.get("max_sources", DEFAULT_MAX_SOURCES)), 10))
    except (TypeError, ValueError):
        max_sources = DEFAULT_MAX_SOURCES

    t0 = time.monotonic()
    timing: dict[str, int] = {}

    t_search = time.monotonic()
    try:
        results = await asyncio.wait_for(_run_search(query, max_sources), timeout=SEARCH_TIMEOUT_S)
    except asyncio.TimeoutError:
        results = []
    timing["search"] = round((time.monotonic() - t_search) * 1000)
    if not results:
        raise ResearchError("no_results")

    # Dynamic, not fixed at SYNTHESIS_TIMEOUT_S (2026-09-28: measured real
    # mistral-nemo/llama-3.3-70b latency for this prompt size at 1.4-11.7s
    # across repeated identical calls - a fixed 1.5s cap produced a ~5%
    # success rate (1/20 on the exact 10-question x2-trial measurement this
    # route was built to pass) because that ceiling almost never reflects
    # what these free-tier models actually take. Search is typically much
    # faster (0.6-2.8s measured) than its own SEARCH_TIMEOUT_S budget, so
    # give synthesis whatever's actually left of the total deadline, capped
    # at SYNTHESIS_TIMEOUT_S only as an upper bound - never less generous
    # than the spec's 1.5s floor when search took its full budget, often
    # much more when search was fast. Same "remaining budget" pattern
    # /token-risk's Jev step already uses successfully.
    synthesis_budget = max(SYNTHESIS_TIMEOUT_S, GLOBAL_DEADLINE_S - (time.monotonic() - t0))
    t_synth = time.monotonic()
    try:
        answer, model_served, cost_usd = await asyncio.wait_for(
            _synthesize(query, results), timeout=synthesis_budget
        )
    except (asyncio.TimeoutError, OpenRouterError) as exc:
        timing["synthesis"] = round((time.monotonic() - t_synth) * 1000)
        raise ResearchError("synthesis_failed") from exc
    timing["synthesis"] = round((time.monotonic() - t_synth) * 1000)

    remaining = GLOBAL_DEADLINE_S - (time.monotonic() - t0)
    claims_verified = False
    claims_verified_reason = None
    t_verify = time.monotonic()
    if remaining >= VERIFY_MIN_BUDGET_S:
        try:
            claims_verified = await asyncio.wait_for(_verify_claims(query, answer, results), timeout=remaining)
            if not claims_verified:
                claims_verified_reason = "jev_found_unsupported_claim"
        except (asyncio.TimeoutError, JevError):
            claims_verified_reason = "verification_timed_out_or_failed"
    else:
        claims_verified_reason = "insufficient_time_budget_remaining"
    timing["verify"] = round((time.monotonic() - t_verify) * 1000)
    timing["total"] = round((time.monotonic() - t0) * 1000)

    return {
        "query": query,
        "answer": answer,
        "sources": _shape_sources(results),
        "claims_verified": claims_verified,
        "claims_verified_reason": claims_verified_reason,
        "timing_ms": timing,
        "_model_served": model_served,
        "_cost_usd": cost_usd,
    }


SAMPLE_RESPONSE = {
    "query": "What caused the 2026 Base network congestion in September?",
    "answer": (
        "Base experienced elevated congestion in mid-September 2026 driven by a surge in memecoin launch "
        "activity on Uniswap V2/V3 and Aerodrome [1]. Average gas prices briefly spiked above typical levels "
        "during peak trading windows [1][2]. The Base team noted no protocol-level incident and attributed the "
        "load to organic demand rather than an attack [2]. Several DEX aggregators reported temporarily degraded "
        "quote latency during the same window [3]. Network conditions normalized within about a day as launch "
        "volume subsided [1]."
    ),
    "sources": [
        {"title": "Base network activity report", "url": "https://example.com/base-report", "published_at": "2026-09-15T00:00:00Z", "source": "web"},
        {"title": "Gas price tracker", "url": "https://example.com/gas-tracker", "published_at": "2026-09-16T00:00:00Z", "source": "web"},
        {"title": "DEX aggregator status page", "url": "https://example.com/dex-status", "published_at": "2026-09-15T00:00:00Z", "source": "web"},
    ],
    "claims_verified": True,
    "claims_verified_reason": None,
    "timing_ms": {"search": 1450, "synthesis": 980, "verify": 720, "total": 3170},
}


@router.get("/research/sample", openapi_extra={"security": []})
async def research_sample():
    return {
        **SAMPLE_RESPONSE,
        "note": "Static example, not a live call.",
        "x402_receipt": make_receipt(None, "research", 3170, 0.0),
    }


async def _paid(request: Request, body: dict):
    payer = extract_payer_address(request)
    user_agent = request.headers.get("user-agent")
    body_excerpt = json.dumps(body)[:2000]
    try:
        with Timer() as timer:
            result = await _lookup(body)
    except ResearchError as exc:
        reason = str(exc)
        if reason == "missing_query":
            code = 400
        elif reason == "no_results":
            code = 504
        else:  # synthesis_failed
            code = 502
        if payer is not None:
            db.log_request(
                route="research", method=request.method, status="error", payer=payer,
                user_agent=user_agent, body_excerpt=body_excerpt, error_reason=reason,
            )
        return JSONResponse({"error": {"reason": reason}}, status_code=code)

    model_served = result.pop("_model_served")
    cost_usd = result.pop("_cost_usd")

    if payer is not None:
        price = effective_price(payer, price_float(config.PRICE_RESEARCH))
        db.log_request(
            route="research", method=request.method, status="paid",
            latency_ms=timer.elapsed_ms, amount_usdc=price, payer=payer,
            user_agent=user_agent, body_excerpt=body_excerpt,
            upstream_cost_usd=cost_usd, margin_usd=(price - cost_usd) if cost_usd is not None else None,
        )
    else:
        # See app/handlers/search.py's 2026-09-28 fix - a verified x402
        # payment always yields a payer; None means a direct/test call to
        # this handler, never real traffic. Logged as "test", never counted
        # as revenue (db.history_7d's payer filter already requires one).
        price = 0.0
        db.log_request(
            route="research", method=request.method, status="test",
            latency_ms=timer.elapsed_ms, payer=None,
            user_agent=user_agent, body_excerpt=body_excerpt,
        )

    return {
        **result,
        "x402_receipt": make_receipt(model_served, "research", timer.elapsed_ms, price),
    }


@router.post("/research", description=ROUTE_DESCRIPTIONS["research"])
async def research_post(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    return await _paid(request, body)
