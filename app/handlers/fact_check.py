import asyncio
import json
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app import config, db
from app.receipts import Timer, effective_price, extract_payer_address, make_receipt, neutral_model_id, price_float
from app.upstream.anthropic import AnthropicCardError, generate_fact_check_queries, generate_fact_check_verdict
from app.upstream.ollama import OllamaError, chat_json
from app.upstream.websearch import SearchError, run_web_search
from app.x402_setup import ROUTE_DESCRIPTIONS

router = APIRouter()

# 2026-10-12: Claude Haiku 5.5 (app/upstream/anthropic.py) is now the
# primary engine for both LLM steps below - short per-call timeouts so an
# unavailable/slow Claude falls back to the local Ollama pipeline quickly
# rather than eating into the overall GLOBAL_DEADLINE_S budget. Normal
# case (Claude up): web search (~1-2s) + query planning (~1-3s) + verdict
# (~2-4s) - comfortably under the <8s target. Degraded case (Claude down,
# Ollama fallback): same CPU-only pipeline as before, still bounded by
# GLOBAL_DEADLINE_S below.
CLAUDE_QUERIES_TIMEOUT_S = 8.0
CLAUDE_VERDICT_TIMEOUT_S = 10.0

MAX_QUERIES = 3
# 2026-10-12: lowered 5 -> 3. Root cause of the timeout incidents below is
# proven, not inferred: Ollama on this VPS is CPU-only (no GPU), confirmed
# via `ollama[...]: tg = 5.01 t/s` in its own log during a live profile -
# the verdict call alone generated 258 tokens in 88.6s at that rate (decode
# time is ~tokens/5, independent of prompt size). The model echoes every
# source's url+stance back in its JSON output, so fewer merged sources
# means fewer generated tokens, not just a shorter prompt - this is the
# one lever that actually shortens DECODE time, which prompt trimming
# alone cannot touch. No code change fixes the 5 t/s ceiling itself; that
# needs a GPU or a faster/hosted model, which is an infra decision, not
# something this fix attempts.
RESULTS_PER_QUERY = 3

# 2026-10-10/11/12: three fixes to the same underlying problem, in order.
#
# (1) 2026-10-10: a bootstrap run showed 3 real attempts taking 134.8s/
# 143.0s/161.6s. nginx's own access log proved what the buyer actually
# got for the one that settled payment: "POST /fact-check 499 0 60.055" -
# a 499 (client already gone) with a ZERO-byte body at 60.055s, while our
# backend kept running for another ~83s and would have returned a real
# result to nobody. A real $0.01 charge for an empty response - billing
# without delivery, proven by the access log. First fix capped the
# pipeline at 60.0s to match the client's own timeout - wrong, since that
# only RACES the client rather than reliably beating it.
#
# (2) 2026-10-11: lowered to 45.0s. Then Lumiere's own 24h probe (same
# claim every time) showed 5/5 real attempts hitting that exact timeout,
# spread across the full day, not clustered around any bootstrap run -
# proof this was never a one-off contention spike.
#
# (3) 2026-10-12: root-caused properly via a live in-container profile
# (app.handlers.fact_check._plan_search_queries/_multi_search/
# _request_verdict timed individually) plus Ollama's own log line at the
# same moment: "tg = 5.01 t/s" - Ollama on this VPS is CPU-only, no GPU.
# Decode time is tokens-generated / 5, independent of prompt size, and
# the model echoes every source's url+stance back in its JSON output, so
# MAX_QUERIES/RESULTS_PER_QUERY (fewer sources to echo) is the one lever
# that actually shortens decode, not just prompt length. Lowering
# RESULTS_PER_QUERY 5->3 and the per-source extract 500->300 chars cut
# the worst-case comparative claim (same one Lumiere probes) from 126.9s
# to 69.1s in a second live profile - real, but still over 45s. No code
# change fixes the 5 t/s ceiling itself; that needs a GPU or a faster/
# hosted model, an infra decision out of scope here.
#
# 90s balances the two real risks instead of only optimizing one: (a)
# below 90s, most real fact-check calls on this CPU-only Ollama would
# simply always time out (safe, but a route that essentially never
# works); (b) above it risks billing a buyer whose own client times out
# first, same failure mode as (1) - 90s stays well under nginx's 120s
# proxy_read_timeout, and under the x402 "maxTimeoutSeconds": 300 this
# resource itself declares in its 402 challenge (the protocol's own
# stated expectation for how long a buyer's client should wait) - but a
# buyer using a shorter client timeout (as low as the 60s one that caused
# incident (1)) remains exposed to the same risk, now with a smaller
# window than before rather than none. Never charged on timeout - same
# contract as every other route here.
GLOBAL_DEADLINE_S = 90.0

# /fact-check/sample documente la forme de sortie pour les sondes (x402watch,
# hermes-contact-discovery). Le pipeline live prend 14–47 s à froid et peut
# bloquer indéfiniment si SearXNG ou Ollama ne répond pas sur le VPS — ce qui
# produit des 499 côté client. La claim est fixe ; on renvoie donc une réponse
# figée instantanée. POST /fact-check reste toujours recalculé.
FACT_CHECK_SAMPLE_RESPONSE: dict = {
    "claim": "The Eiffel Tower is taller than the Statue of Liberty.",
    "verdict": "supported",
    "confidence": 0.95,
    "sources": [
        {
            "url": "https://homework.study.com/explanation/is-eiffel-tower-taller-than-the-statue-of-liberty.html",
            "title": "Is Eiffel Tower taller than the Statue of Liberty? | Homework.Study.com",
            "stance": "supports",
        },
        {
            "url": "https://compareheight.net/eiffel-tower-height-comparison/",
            "title": "Eiffel Tower Height Comparison - CompareHeight.net",
            "stance": "supports",
        },
        {
            "url": "https://www.explore.com/1088063/the-eiffel-tower-and-the-other-tallest-structures-in-the-world/",
            "title": "The Eiffel Tower's Height Compared To Other Iconic Structures - Explore",
            "stance": "supports",
        },
    ],
    "x402_receipt": {
        "model_served": "model-unknown",
        "upstream": "web_search+llm",
        "latency_ms": 1,
        "price_paid_usdc": 0.0,
        "sources_read": 3,
        "searches_run": 1,
    },
}

# Below this, or when no source actually takes a side, the verdict is forced
# to "inconclusive" regardless of what the model claimed (see
# _apply_inconclusive_threshold) - a $0.05 route that answers a true claim
# with a confident-sounding "mixed" when every source it cited was "neutral"
# is worse than one that says it doesn't know (2026-09-06: observed live on
# the Eiffel Tower/Statue of Liberty sample, confidence 0.65, all sources
# neutral - clearly not what "mixed" is supposed to mean).
CONFIDENCE_THRESHOLD = 0.5

# Diagnosed 2026-09-06 (see cm-central brief): the model WAS receiving real
# 500-char page extracts, not just titles - that hypothesis was wrong. The
# actual bug is retrieval, not content depth: a single search using the raw
# claim as the query is dominated by whichever entity is more prominent in
# the text, so a comparative claim ("X is taller than Y") returns 5 sources
# all about X and none about Y - confirmed empirically, searching "Statue of
# Liberty height" alone finds it instantly. Fetching full pages (as first
# proposed) does NOT fix this: the full toureiffel.paris page (3462 chars)
# never mentions the Statue of Liberty either, because that page has no
# reason to. The fix is query planning: ask the model what to search for
# BEFORE searching, not just reusing the claim verbatim.
QUERY_PLANNING_PROMPT = (
    "Given a factual claim, produce 1 to 3 short, focused web search queries "
    "that together would find sources to verify or refute it. If the claim "
    "compares two or more things, you MUST include a separate query for each "
    "thing (e.g. for 'X is taller than Y', queries should be ['X height', "
    "'Y height']) - never just repeat the whole claim as a single query when "
    'a comparison is involved. Respond with ONLY a JSON object: {"queries": '
    '["...", ...]}.'
)

SYSTEM_PROMPT = (
    "You are a fact-checking engine. Given a claim and a set of web search "
    "results, determine whether the claim is supported, contradicted, mixed, "
    "or unverifiable based ONLY on the provided sources - never on outside "
    "knowledge. Respond with ONLY a JSON object of the form "
    '{"verdict": "supported"|"contradicted"|"mixed"|"unverifiable", '
    '"confidence": <0.0-1.0>, "sources": [{"url": "...", "stance": '
    '"supports"|"contradicts"|"neutral"}]}. Include every source you were '
    "given, each with its actual stance toward the claim. Do not force a "
    "confident verdict if the sources don't clearly support one - low "
    "confidence and neutral stances are honest, better answers than a "
    "guess."
)

SAMPLE_CLAIM = "The Eiffel Tower is taller than the Statue of Liberty."


class FactCheckError(Exception):
    def __init__(self, reason: str, detail: str | None = None):
        self.reason = reason
        self.detail = detail
        super().__init__(reason)


async def _plan_search_queries(claim: str) -> tuple[list[str], float]:
    """Ask the model what to search for before searching, rather than
    reusing the claim verbatim as the only query - see the diagnosis above.
    Claude Haiku first (fast); falls back to the local Ollama pipeline on
    ANY AnthropicCardError (not configured, breaker open, over monthly
    budget, timeout, malformed response), then to the raw claim if that
    ALSO fails - three tiers, so a planning hiccup degrades gracefully
    instead of blocking the whole route. Returns (queries, cost_usd) -
    cost is 0.0 whenever Claude wasn't the one that answered."""
    try:
        queries, cost = await generate_fact_check_queries(claim, timeout_s=CLAUDE_QUERIES_TIMEOUT_S)
        return queries, cost
    except AnthropicCardError:
        pass

    messages = [
        {"role": "system", "content": QUERY_PLANNING_PROMPT},
        {"role": "user", "content": claim},
    ]
    try:
        parsed = await chat_json(messages, max_tokens=100)
        queries = parsed.get("queries")
        if isinstance(queries, list) and queries and all(isinstance(q, str) and q.strip() for q in queries):
            return [q.strip() for q in queries[:MAX_QUERIES]], 0.0
    except (OllamaError, AttributeError):
        pass
    return [claim], 0.0


async def _multi_search(queries: list[str]) -> tuple[list[dict], str | None]:
    """Runs EVERY query fully (unlike app.handlers.search._run_batch_search,
    which caps at a global result count and can silently starve later
    queries if the first alone fills it) - a comparative claim needs sources
    on every side of the comparison, not just whichever query ran first.

    Queries run concurrently, not sequentially: with up to MAX_QUERIES=3 and
    SEARXNG_TIMEOUT_SECONDS=15 each, a sequential loop's worst case (45s) ate
    most of nginx's 120s budget before the verdict call (up to 100s) even
    started - the cause of the 499s observed on this route (2026-09-11).
    Concurrent calls cap the search phase at ~15s regardless of query count."""
    outcomes = await asyncio.gather(
        *(run_web_search(q, RESULTS_PER_QUERY) for q in queries), return_exceptions=True
    )
    seen_urls: set[str] = set()
    merged: list[dict] = []
    model_served: str | None = None
    last_error: BaseException | None = None
    for outcome in outcomes:
        if isinstance(outcome, BaseException):
            last_error = outcome
            continue
        results, model = outcome
        model_served = model_served or model
        for r in results:
            if r["url"] in seen_urls:
                continue
            seen_urls.add(r["url"])
            merged.append(r)
    if not merged and last_error is not None:
        raise last_error
    return merged, model_served


def _parse_verdict_response(parsed: dict) -> dict:
    verdict = parsed["verdict"]
    confidence = float(parsed["confidence"])
    sources = parsed["sources"]
    if verdict not in ("supported", "contradicted", "mixed", "unverifiable", "inconclusive"):
        raise ValueError("invalid verdict")
    if not isinstance(sources, list):
        raise ValueError("sources not a list")
    return {"verdict": verdict, "confidence": confidence, "sources": sources}


async def _request_verdict(claim: str, sources_context: str) -> tuple[dict, float, str]:
    """Claude Haiku 5.5 first (app/upstream/anthropic.py - same module-
    level budget/breaker state as POST /token-card, so both share one
    monthly cap and one breaker). Falls back to the local gemma3:4b
    pipeline (app/upstream/ollama.py) on ANY AnthropicCardError - unlike
    /token-card, this route is NOT wired to app/capacity.py's
    TokenCardCircuitBreakerMiddleware (that stays exact-path-matched to
    POST /token-card only): fact-check always has a fully functional
    fallback, so a Claude outage degrades the route rather than refusing
    the sale. Returns (verdict_dict, cost_usd, source) where source is
    "claude" or "ollama" - the caller surfaces this as model_served."""
    try:
        verdict, cost = await generate_fact_check_verdict(claim, sources_context, timeout_s=CLAUDE_VERDICT_TIMEOUT_S)
        return verdict, cost, "claude"
    except AnthropicCardError:
        pass

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"Claim: {claim}\n\nSources:\n{sources_context}"},
    ]
    # 100s, pas le defaut de 60s : le jugement (gemma3:4b, CPU-only, ~5
    # tokens/sec mesure sur ce VPS - voir GLOBAL_DEADLINE_S) peut depasser
    # 60s, sous le budget nginx proxy_read_timeout de 120s pour laisser de
    # la marge a la recherche qui le precede. N'est plus le chemin normal
    # depuis que Claude est tente en premier - uniquement le secours.
    parsed = await chat_json(messages, max_tokens=500, timeout=100.0)
    return _parse_verdict_response(parsed), 0.0, "ollama"


async def _check_claim(claim: str) -> tuple[dict, str | None, int, int, float]:
    queries, queries_cost = await _plan_search_queries(claim)
    try:
        results, _search_model = await _multi_search(queries)
    except (OllamaError, SearchError) as exc:
        raise FactCheckError("upstream_error", str(exc)[:200])
    if not results:
        raise FactCheckError("no_sources_found")

    sources_context = "\n\n".join(
        f"[{i + 1}] {r['title'] or r['url']} ({r['url']}): {(r['extract'] or '')[:300]}"
        for i, r in enumerate(results)
    )
    try:
        verdict_result, verdict_cost, verdict_source = await _request_verdict(claim, sources_context)
    except OllamaError as exc:
        raise FactCheckError("upstream_error", str(exc)[:200])
    except (KeyError, TypeError, ValueError) as exc:
        raise FactCheckError("verification_failed", f"malformed model response: {exc}"[:200])

    model_served = config.ANTHROPIC_MODEL if verdict_source == "claude" else config.OLLAMA_MODEL
    anthropic_cost = queries_cost + verdict_cost
    verdict = verdict_result["verdict"]
    confidence = verdict_result["confidence"]
    sources = verdict_result["sources"]

    # Enrich with titles from the real search results, keyed by URL - a
    # model-invented URL not among the actual sources it was given is
    # dropped rather than surfaced without provenance.
    by_url = {r["url"]: r for r in results}
    enriched_sources = []
    for s in sources:
        url = s.get("url") if isinstance(s, dict) else None
        if url not in by_url:
            continue
        stance = s.get("stance")
        enriched_sources.append({
            "url": url,
            "title": by_url[url]["title"],
            "stance": stance if stance in ("supports", "contradicts", "neutral") else "neutral",
        })
    if not enriched_sources:
        raise FactCheckError("verification_failed", "model did not reference any real source URL")

    confidence = max(0.0, min(1.0, confidence))
    has_clear_stance = any(s["stance"] in ("supports", "contradicts") for s in enriched_sources)
    if verdict != "inconclusive" and (confidence < CONFIDENCE_THRESHOLD or not has_clear_stance):
        # Mechanical override, not a suggestion to the model - a route this
        # cheap can't rely on the model policing its own overconfidence.
        verdict = "inconclusive"

    result = {
        "verdict": verdict,
        "confidence": confidence,
        "sources": enriched_sources,
    }
    return result, model_served, len(results), len(queries), anthropic_cost


@router.get("/fact-check/sample", openapi_extra={"security": []})
async def fact_check_sample():
    return FACT_CHECK_SAMPLE_RESPONSE


async def _handle_fact_check(request: Request, body: dict):
    method = request.method
    payer = extract_payer_address(request)
    user_agent = request.headers.get("user-agent")
    body_excerpt = json.dumps(body)[:2000]
    claim = body.get("claim")
    if not claim or not isinstance(claim, str):
        db.log_request(
            route="fact-check", method=method, status="error", payer=payer,
            user_agent=user_agent, body_excerpt=body_excerpt, error_reason="missing_claim",
        )
        return JSONResponse({"error": {"reason": "missing_claim"}}, status_code=400)

    try:
        with Timer() as t:
            result, model_served, sources_read, queries_run, anthropic_cost = await asyncio.wait_for(
                _check_claim(claim), timeout=GLOBAL_DEADLINE_S
            )
    except asyncio.TimeoutError:
        db.log_request(
            route="fact-check", method=method, status="error", payer=payer,
            user_agent=user_agent, body_excerpt=body_excerpt, error_reason="timeout",
        )
        return JSONResponse(
            {"error": {"reason": "timeout", "detail": f"No response within {GLOBAL_DEADLINE_S:.0f}s - not charged."}},
            status_code=504,
        )
    except FactCheckError as exc:
        db.log_request(
            route="fact-check", method=method, status="error", payer=payer,
            user_agent=user_agent, body_excerpt=body_excerpt, error_reason=exc.reason,
        )
        status_code = 400 if exc.reason == "no_sources_found" else 502
        return JSONResponse({"error": {"reason": exc.reason, "detail": exc.detail}}, status_code=status_code)

    price = effective_price(payer, price_float(config.PRICE_FACT_CHECK))
    db.log_request(
        route="fact-check", method=method, status="paid", latency_ms=t.elapsed_ms,
        amount_usdc=price, payer=payer, user_agent=user_agent, body_excerpt=body_excerpt,
        upstream_cost_usd=anthropic_cost,
    )
    receipt = make_receipt(
        neutral_model_id(model_served), "web_search+llm", t.elapsed_ms, price,
        searches_run=queries_run, sources_read=sources_read,
    )
    return {"claim": claim, **result, "x402_receipt": receipt}


@router.get("/fact-check", description=ROUTE_DESCRIPTIONS["fact-check"])
async def fact_check_get(request: Request):
    params = dict(request.query_params)
    body = params if params else {"claim": SAMPLE_CLAIM}
    return await _handle_fact_check(request, body)


@router.post("/fact-check", description=ROUTE_DESCRIPTIONS["fact-check"])
async def fact_check(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    return await _handle_fact_check(request, body)
