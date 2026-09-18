import asyncio
import json
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app import config, db
from app.receipts import Timer, effective_price, extract_payer_address, make_receipt, neutral_model_id, price_float
from app.upstream.ollama import OllamaError, chat_json
from app.upstream.websearch import SearchError, run_web_search
from app.x402_setup import ROUTE_DESCRIPTIONS

router = APIRouter()

MAX_QUERIES = 3
RESULTS_PER_QUERY = 5

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


async def _plan_search_queries(claim: str) -> list[str]:
    """Ask the model what to search for before searching, rather than
    reusing the claim verbatim as the only query - see the diagnosis above.
    Falls back to the raw claim (the old behavior) if planning fails for any
    reason, so a query-planning hiccup degrades gracefully instead of
    blocking the whole route."""
    messages = [
        {"role": "system", "content": QUERY_PLANNING_PROMPT},
        {"role": "user", "content": claim},
    ]
    try:
        parsed = await chat_json(messages, max_tokens=150)
        queries = parsed.get("queries")
        if isinstance(queries, list) and queries and all(isinstance(q, str) and q.strip() for q in queries):
            return [q.strip() for q in queries[:MAX_QUERIES]]
    except (OllamaError, AttributeError):
        pass
    return [claim]


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


async def _request_verdict(claim: str, sources_context: str) -> dict:
    """Jugement par gemma3:4b en local (app/upstream/ollama.py) plutot que par
    OpenRouter - remplace un modele gratuit distant, dependant d'un solde de
    compte, par un modele local sans quota ni cle. Une seule tentative : pas
    de modele de secours a essayer, contrairement au montage OpenRouter
    d'origine (models -> last-resort)."""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"Claim: {claim}\n\nSources:\n{sources_context}"},
    ]
    # 100s, pas le defaut de 60s : le jugement (800 tokens, gemma3:4b sur ce
    # VPS) a depasse 60s sur au moins un claim reel (2026-09-10, ollama_timeout
    # -> 502), sous le budget nginx proxy_read_timeout de 120s pour laisser de
    # la marge a la recherche qui le precede.
    parsed = await chat_json(messages, max_tokens=800, timeout=100.0)
    return _parse_verdict_response(parsed)


async def _check_claim(claim: str) -> tuple[dict, str | None, int, int]:
    queries = await _plan_search_queries(claim)
    try:
        results, _search_model = await _multi_search(queries)
    except (OllamaError, SearchError) as exc:
        raise FactCheckError("upstream_error", str(exc)[:200])
    if not results:
        raise FactCheckError("no_sources_found")

    sources_context = "\n\n".join(
        f"[{i + 1}] {r['title'] or r['url']} ({r['url']}): {(r['extract'] or '')[:500]}"
        for i, r in enumerate(results)
    )
    try:
        verdict_result = await _request_verdict(claim, sources_context)
    except OllamaError as exc:
        raise FactCheckError("upstream_error", str(exc)[:200])
    except (KeyError, TypeError, ValueError) as exc:
        raise FactCheckError("verification_failed", f"malformed model response: {exc}"[:200])

    model_served = config.OLLAMA_MODEL
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
    return result, model_served, len(results), len(queries)


@router.get("/fact-check/sample", openapi_extra={"security": []})
async def fact_check_sample():
    return FACT_CHECK_SAMPLE_RESPONSE


@router.post("/fact-check", description=ROUTE_DESCRIPTIONS["fact-check"])
async def fact_check(request: Request):
    payer = extract_payer_address(request)
    user_agent = request.headers.get("user-agent")
    try:
        body = await request.json()
    except Exception:
        body = {}
    body_excerpt = json.dumps(body)[:2000]
    claim = body.get("claim")
    if not claim or not isinstance(claim, str):
        db.log_request(
            route="fact-check", method="POST", status="error", payer=payer,
            user_agent=user_agent, body_excerpt=body_excerpt, error_reason="missing_claim",
        )
        return JSONResponse({"error": {"reason": "missing_claim"}}, status_code=400)

    try:
        with Timer() as t:
            result, model_served, sources_read, queries_run = await _check_claim(claim)
    except FactCheckError as exc:
        db.log_request(
            route="fact-check", method="POST", status="error", payer=payer,
            user_agent=user_agent, body_excerpt=body_excerpt, error_reason=exc.reason,
        )
        status_code = 400 if exc.reason == "no_sources_found" else 502
        return JSONResponse({"error": {"reason": exc.reason, "detail": exc.detail}}, status_code=status_code)

    price = effective_price(payer, price_float(config.PRICE_FACT_CHECK))
    db.log_request(
        route="fact-check", method="POST", status="paid", latency_ms=t.elapsed_ms,
        amount_usdc=price, payer=payer, user_agent=user_agent, body_excerpt=body_excerpt,
    )
    receipt = make_receipt(
        neutral_model_id(model_served), "web_search+llm", t.elapsed_ms, price,
        searches_run=queries_run, sources_read=sources_read,
    )
    return {"claim": claim, **result, "x402_receipt": receipt}
