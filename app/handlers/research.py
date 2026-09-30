"""POST /research — cited research synthesis on Base, < 4.5s guaranteed.

v2 (2026-09-30, "Jev-powered research"): internal web search (reusing
/search's own pipeline in-process - no self-paid HTTP call, no second page
fetch beyond what that pipeline already fetches) feeds a single fast, PAID
LLM call pinned to a specific provider, with a deterministic no-LLM
extractive fallback if that call ever misses its own tight cap - never a
second LLM call, so the total is bounded by construction, not by hoping an
upstream is fast.

Time-budget orchestrator, same pattern as /token-risk and /search (see
their module docstrings for the incidents that shaped this): t0 = request
received, GLOBAL_DEADLINE_S total.

- Search (internal run_web_search + the same content enrichment /search
  itself uses) gets its own SEARCH_TIMEOUT_S. No search results by then -
  whether genuinely empty or the search timed out - means there is nothing
  to synthesize from, so this is the one hard failure case: 422, unsettled
  (a voluntary "couldn't produce a result", never a 5xx - x402's own
  settlement rule skips any status >= 400 regardless).
- Synthesis gets a FIXED SYNTHESIS_TIMEOUT_S = 1.5s cap. v1 (still in git
  history) tried "mistral-nemo, falling back to llama-3.3-70b" with NO
  provider pinned, and measured 1.4-11.7s latency across repeated identical
  calls - only 3/10 real end-to-end calls succeeded within budget,
  diagnosed 2026-09-30 as OpenRouter silently load-balancing across EVERY
  provider that serves that model id, including slow/degraded ones (one of
  mistral-nemo's own providers, Novita, measured 37.9% uptime the same
  day). The model choice itself was never the problem - a live latency
  test that day, pinning meta-llama/llama-3.3-70b-instruct to ONLY its
  Groq-hosted endpoint (provider={"only": ["Groq"], "allow_fallbacks":
  False}), measured 0.72-1.13s across 5 real calls with a realistic
  research prompt - comfortably inside a 1.5s cap, with Groq's own
  uptime_last_30m at 99.79% that day. That pin is RESEARCH_MODEL /
  RESEARCH_MODEL_PROVIDER below. On a miss (Groq itself times out or
  errors - rare, but not assumed away), there is deliberately no second
  LLM call - retrying anywhere, even a fast provider, risks compounding
  past the deadline. Instead: EXTRACTIVE fallback (_extractive_synthesize)
  - the most relevant sentences already sitting in the search results
  themselves (no upstream call required to have SOME answer), picked by a
  single bounded Jev /rank-style call when enough budget remains, falling
  back further to a deterministic keyword-overlap heuristic (same
  Jev-then-deterministic pattern already used by websearch.py's rerank and
  token_risk.py's verdict) when it doesn't. Every sentence in an extractive
  answer is a verbatim quote from its cited source, so claims_verified is
  trivially true for that path - there is nothing to check that isn't
  already a direct copy.
- Claim verification (a single Jev call checking whether the LLM answer's
  citations are actually supported by their sources - not a per-claim
  check, which would need multiple calls this budget cannot afford) only
  runs for the LLM path, and only when at least VERIFY_MIN_BUDGET_S
  remains after synthesis. Skipped (or timed out) verification still
  settles as 200, with "claims_verified": false and
  "claims_verified_reason" explaining why - never silently presented as
  verified when it wasn't attempted.

Re-enabled in the catalog 2026-09-30 (RESEARCH_ENABLED = True) after being
withdrawn the same day pending exactly this fix - see git history for the
withdrawal commit and the diagnosis that led here.
"""

from __future__ import annotations

import asyncio
import json
import re
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

RESEARCH_ENABLED = True

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
# "no_results" failures live than 2.7-3.0s did).
GLOBAL_DEADLINE_S = 4.2
SEARCH_TIMEOUT_S = 2.7
SYNTHESIS_TIMEOUT_S = 1.5  # fixed cap (2026-09-30) - Groq-pinned llama-3.3-70b measured 0.72-1.13s, no dynamic extension needed
VERIFY_MIN_BUDGET_S = 1.0
JEV_EXTRACTIVE_MIN_BUDGET_S = 0.8  # below this, skip straight to the deterministic sentence picker - no upstream call attempted at all

RESEARCH_MODEL = "meta-llama/llama-3.3-70b-instruct"
# Pinned, not a fallback list (2026-09-30) - see module docstring. Groq is
# the only provider tried; allow_fallbacks=False means a Groq miss goes
# straight to the extractive path below, never silently to a slower
# provider serving the same model id.
RESEARCH_MODEL_PROVIDER = {"only": ["Groq"], "allow_fallbacks": False}
SYNTHESIS_MAX_TOKENS = 350

_SYSTEM_PROMPT = (
    "You are a careful research assistant. Answer using ONLY the numbered "
    "sources given to you. Write 5 to 8 sentences. Immediately after each "
    "claim drawn from a source, cite it in square brackets, e.g. [1] or "
    "[1][2] if two sources support it. If the sources do not fully answer "
    "the question, say so plainly rather than guessing or using outside "
    "knowledge."
)

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_WORD_RE = re.compile(r"[a-z0-9]+")
EXTRACTIVE_SENTENCE_CHARS = 300
EXTRACTIVE_SENTENCES_PER_SOURCE = 6
EXTRACTIVE_MAX_SENTENCES = 6
_EXTRACTIVE_RANK_INSTRUCTIONS = "Which of these sentences is most useful for answering the question?"


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


async def _synthesize_llm(query: str, results: list[dict]) -> tuple[str, str | None, float | None]:
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
        messages, [RESEARCH_MODEL], max_tokens=SYNTHESIS_MAX_TOKENS, temperature=0.2,
        timeout=30.0, extra_body={"usage": {"include": True}, "provider": RESEARCH_MODEL_PROVIDER},
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


def _split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_SPLIT_RE.split(text) if len(s.strip()) >= 25]


def _keyword_overlap_score(query: str, sentence: str) -> int:
    q_tokens = set(_WORD_RE.findall(query.lower()))
    s_tokens = set(_WORD_RE.findall(sentence.lower()))
    return len(q_tokens & s_tokens)


async def _jev_rank_sentences(query: str, candidates: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """Same Jev 'choice over many criteria' primitive as POST /rank
    (app/handlers/jev.py) - one call, probabilities over every candidate
    sentence, sorted descending. Reused rather than reinvented so this
    inherits the same well-tested shape."""
    criteria = {f"s_{i}": sent[:EXTRACTIVE_SENTENCE_CHARS] for i, (_, sent) in enumerate(candidates)}
    questions = {"ranking": {"type": "choice", "instructions": _EXTRACTIVE_RANK_INSTRUCTIONS, "criteria": criteria}}
    data = await ask_jev(query, questions)
    probabilities = data["answers"]["ranking"]["probabilities"]
    ranked = sorted(probabilities.items(), key=lambda kv: kv[1], reverse=True)
    return [candidates[int(key.split("_")[1])] for key, _ in ranked]


async def _extractive_synthesize(query: str, results: list[dict], budget_s: float) -> str:
    """No-LLM fallback: the most relevant sentences already sitting in the
    search results, picked by a bounded Jev call when enough budget
    remains (JEV_EXTRACTIVE_MIN_BUDGET_S), otherwise straight to a
    deterministic keyword-overlap sort - never a second LLM call, and
    never without SOME answer as long as at least one source has
    extractable text. Every sentence returned is a verbatim quote from its
    cited source."""
    candidates: list[tuple[int, str]] = []
    for i, r in enumerate(results, start=1):
        for sent in _split_sentences(_source_text(r))[:EXTRACTIVE_SENTENCES_PER_SOURCE]:
            candidates.append((i, sent))

    if not candidates:
        return "The retrieved sources did not contain extractable sentences to answer this question."

    ranked: list[tuple[int, str]] | None = None
    if budget_s >= JEV_EXTRACTIVE_MIN_BUDGET_S:
        try:
            ranked = await asyncio.wait_for(_jev_rank_sentences(query, candidates), timeout=budget_s)
        except (asyncio.TimeoutError, JevError):
            ranked = None
    if ranked is None:
        ranked = sorted(candidates, key=lambda c: _keyword_overlap_score(query, c[1]), reverse=True)

    chosen = ranked[:EXTRACTIVE_MAX_SENTENCES]
    return " ".join(f"{sent} [{idx}]" for idx, sent in chosen)


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

    t_synth = time.monotonic()
    model_served = None
    cost_usd = None
    try:
        answer, model_served, cost_usd = await asyncio.wait_for(
            _synthesize_llm(query, results), timeout=SYNTHESIS_TIMEOUT_S
        )
        synthesis_mode = "llm"
    except (asyncio.TimeoutError, OpenRouterError):
        remaining_for_extractive = max(0.0, GLOBAL_DEADLINE_S - (time.monotonic() - t0))
        answer = await _extractive_synthesize(query, results, remaining_for_extractive)
        synthesis_mode = "extractive"
    timing["synthesis"] = round((time.monotonic() - t_synth) * 1000)

    if synthesis_mode == "extractive":
        # Every sentence is a verbatim quote from its cited source - there
        # is nothing an unsupported-claim check could catch that isn't
        # already a direct copy, so this is trivially true, not skipped.
        claims_verified = True
        claims_verified_reason = None
        timing["verify"] = 0
    else:
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
        "synthesis": synthesis_mode,
        "sources": _shape_sources(results),
        "claims_verified": claims_verified,
        "claims_verified_reason": claims_verified_reason,
        "timing_ms": timing,
        "_model_served": model_served,
        "_cost_usd": cost_usd,
    }


# Captured from a real end-to-end call (2026-09-30, no payment, see
# app.handlers.research._lookup - not hand-written).
SAMPLE_RESPONSE = {
    "query": "What are the main features of the Rust programming language?",
    "answer": (
        "The main features of the Rust programming language include an emphasis on performance, type safety, "
        "concurrency, and memory safety [1]. Rust supports multiple programming paradigms [1]. The language's "
        "syntax is heavily influenced by C++ and functional programming languages such as OCaml [2]. Rust has a "
        "focus on static typing and a borrow system, similar to other systems programming languages [5]. However, "
        "sources [3] and [4] do not provide information about Rust, instead discussing other programming "
        "languages, Zig and V, respectively. Overall, the sources suggest that Rust is a systems programming "
        "language with a strong focus on safety and performance [1][2][5]."
    ),
    "synthesis": "llm",
    "sources": [
        {"title": "Rust (programming language)", "url": "https://en.wikipedia.org/wiki/Rust_(programming_language)", "published_at": None, "source": "wikipedia"},
        {"title": "Rust syntax", "url": "https://en.wikipedia.org/wiki/Rust_syntax", "published_at": None, "source": "wikipedia"},
        {"title": "Zig (programming language)", "url": "https://en.wikipedia.org/wiki/Zig_(programming_language)", "published_at": None, "source": "wikipedia"},
        {"title": "V (programming language)", "url": "https://en.wikipedia.org/wiki/V_(programming_language)", "published_at": None, "source": "wikipedia"},
        {"title": "Mojo (programming language)", "url": "https://en.wikipedia.org/wiki/Mojo_(programming_language)", "published_at": None, "source": "wikipedia"},
    ],
    "claims_verified": True,
    "claims_verified_reason": None,
    "timing_ms": {"search": 1230, "synthesis": 644, "verify": 287, "total": 2160},
}


@router.get("/research/sample", openapi_extra={"security": []})
async def research_sample():
    return {
        **SAMPLE_RESPONSE,
        "note": "Static example, not a live call.",
        "x402_receipt": make_receipt(None, "research", SAMPLE_RESPONSE["timing_ms"]["total"], 0.0),
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
        elif reason in ("no_results", "synthesis_failed"):
            # 422, not 5xx: both are "couldn't produce a result within
            # budget", not an infrastructure fault - same reasoning as
            # /search's own no_results fix, and x402's own settlement rule
            # already skips settlement on any status >= 400 regardless, so
            # this changes nothing about "aucun règlement".
            code = 422
        else:
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
