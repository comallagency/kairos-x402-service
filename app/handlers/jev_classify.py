"""Shared engine for Pack 1's 7 text-classification routes (POST /sentiment,
/classify, /intent, /spam-check, /toxicity, /language, /pii-check).

Jev is the primary engine (JEV_TIMEOUT_S cap). On a Jev miss (slow or
error), falls back to a single Groq-pinned llama-3.3-70b-instruct call
(LLM_TIMEOUT_S cap), asked to answer in the same shape via a strict JSON
instruction - "engine": "jev" | "fallback" in the response tells the buyer
which one actually answered. Never both, never a second LLM call - same
"one primary engine, one bounded fallback, no compounding retries"
discipline as research.py's synthesis step, so the total is bounded by
construction (JEV_TIMEOUT_S + LLM_TIMEOUT_S = 3.0s worst case, comfortably
under the 4.5s guarantee with real margin for network/ASGI overhead).

Each route file defines its own `_lookup(body) -> dict` (pure logic, no
logging) and calls respond_http() from its FastAPI route - the same
lookup()/_paid() split every other handler in this codebase already uses,
so the same _lookup can be reused unchanged by app/mcp_server.py's MCP
tool wrappers.
"""

from __future__ import annotations

import asyncio
import json

from fastapi.responses import JSONResponse

from app import db
from app.receipts import Timer, effective_price, extract_payer_address, make_receipt, price_float
from app.upstream.jev import JevError, ask_jev
from app.upstream.openrouter import OpenRouterError, chat_completion

JEV_TIMEOUT_S = 1.5
LLM_TIMEOUT_S = 1.5
MAX_TEXT_CHARS = 4000

FALLBACK_MODEL = "meta-llama/llama-3.3-70b-instruct"
# Same pin as research.py's RESEARCH_MODEL_PROVIDER - Groq measured
# 0.72-1.13s for this exact model on 2026-09-30, comfortably inside
# LLM_TIMEOUT_S, and allow_fallbacks=False means a Groq miss surfaces as an
# OpenRouterError immediately rather than silently drifting to a slower
# provider serving the same model id.
FALLBACK_MODEL_PROVIDER = {"only": ["Groq"], "allow_fallbacks": False}


class ClassifyError(Exception):
    pass


def require_text(body: dict, field: str = "text") -> str:
    text = body.get(field) if isinstance(body, dict) else None
    if not isinstance(text, str) or not text.strip():
        raise ClassifyError("empty_text")
    return text.strip()[:MAX_TEXT_CHARS]


def _top_two(probabilities: dict) -> tuple[str, float, str | None, float | None]:
    """Rounds to 2 decimals (2026-09-30, quality pass) - Jev's real
    probabilities already vary continuously for ambiguous input (verified:
    5 real ambiguous texts produced e.g. 0.64/0.36, 0.89/0.01/0.1, never a
    degenerate 0/1 split unless the input genuinely was that clear-cut),
    this only fixes DISPLAY precision. round(x, 2) explicitly - not the
    bare round(x), which would collapse to an integer 0 or 1."""
    ranked = sorted(probabilities.items(), key=lambda kv: kv[1], reverse=True)
    label, prob = ranked[0]
    alt_label, alt_prob = ranked[1] if len(ranked) > 1 else (None, None)
    return label, round(prob, 2), alt_label, (round(alt_prob, 2) if alt_prob is not None else None)


async def _classify_via_jev(text: str, instructions: str, criteria: dict) -> dict:
    questions = {"label": {"type": "choice", "instructions": instructions, "criteria": criteria}}
    data = await ask_jev(text, questions)
    answer = data["answers"]["label"]
    label, prob, alt_label, alt_prob = _top_two(answer["probabilities"])
    return {
        "label": label,
        "probability": prob,
        "alternate_label": alt_label,
        "alternate_probability": alt_prob,
        "engine": "jev",
    }


_FALLBACK_SYSTEM = (
    "You are a strict text classifier. Given a piece of text and a fixed "
    "set of labels, choose exactly one label and a confidence between 0 "
    "and 1. Reply with ONLY a JSON object, no other text: "
    '{"label": "<one of the given labels, exactly as given>", "probability": <0-1>}'
)


async def _classify_via_llm(text: str, instructions: str, labels: list[str]) -> dict:
    prompt = f"{instructions}\nLabels: {', '.join(labels)}\n\nText:\n{text}"
    messages = [
        {"role": "system", "content": _FALLBACK_SYSTEM},
        {"role": "user", "content": prompt},
    ]
    data = await chat_completion(
        messages, [FALLBACK_MODEL], max_tokens=60, temperature=0.0,
        timeout=30.0, extra_body={"provider": FALLBACK_MODEL_PROVIDER},
        # Intentionally NOT LLM_TIMEOUT_S here - same reasoning as
        # research.py's _synthesize_llm: the outer asyncio.wait_for in
        # classify() already enforces that bound by cancelling this call
        # cleanly, and passing the same value to httpx's own timeout too
        # would race it against that cancellation.
    )
    raw = data["choices"][0]["message"]["content"].strip()
    try:
        parsed = json.loads(raw)
        label = str(parsed["label"])
        prob = float(parsed["probability"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise OpenRouterError(f"fallback classifier returned unparseable output: {raw[:200]}") from exc
    if label not in labels:
        lowered = {l.lower(): l for l in labels}
        label = lowered.get(label.lower(), labels[0])
    remaining = [l for l in labels if l != label]
    return {
        "label": label,
        "probability": round(max(0.0, min(1.0, prob)), 2),
        "alternate_label": remaining[0] if remaining else None,
        "alternate_probability": None,
        "engine": "fallback",
    }


async def classify(text: str, instructions: str, criteria: dict) -> dict:
    """criteria: {label: description, ...} - same shape every ask_jev
    'choice' question already uses elsewhere in this codebase (decide.py,
    guard, verify). Tries Jev first (JEV_TIMEOUT_S cap); on a timeout or
    JevError, falls back to a single Groq-pinned LLM call (LLM_TIMEOUT_S
    cap) - never both, never a second LLM call."""
    try:
        return await asyncio.wait_for(_classify_via_jev(text, instructions, criteria), timeout=JEV_TIMEOUT_S)
    except (asyncio.TimeoutError, JevError):
        pass
    try:
        return await asyncio.wait_for(
            _classify_via_llm(text, instructions, list(criteria.keys())), timeout=LLM_TIMEOUT_S
        )
    except (asyncio.TimeoutError, OpenRouterError) as exc:
        raise ClassifyError("engine_unavailable") from exc


async def respond_http(request, body: dict, *, route: str, price_str: str, lookup):
    """Shared HTTP tail: lookup is an async callable taking no arguments,
    already bound to this call's own validated text/criteria by the
    caller's own _lookup(body). Identical across all 7 routes - only the
    lookup differs per route."""
    payer = extract_payer_address(request)
    user_agent = request.headers.get("user-agent")
    body_excerpt = json.dumps(body)[:2000] if isinstance(body, dict) else ""
    try:
        with Timer() as timer:
            result = await lookup()
    except ClassifyError as exc:
        db.log_request(
            route=route, method="POST", status="error", payer=payer,
            user_agent=user_agent, body_excerpt=body_excerpt, error_reason=str(exc),
        )
        # 422, unsettled: x402's own settlement rule skips any status >= 400
        # regardless - "couldn't produce a result", never a 5xx.
        return JSONResponse({"error": {"reason": str(exc)}}, status_code=422)

    price = effective_price(payer, price_float(price_str))
    db.log_request(
        route=route, method="POST", status="paid", latency_ms=timer.elapsed_ms,
        amount_usdc=price, payer=payer, user_agent=user_agent, body_excerpt=body_excerpt,
    )
    return {**result, "x402_receipt": make_receipt(None, route, timer.elapsed_ms, price)}
