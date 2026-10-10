"""POST /token-card - shareable AI verdict card for a Base ERC-20, built on
top of POST /token-risk's existing on-chain analysis (app.handlers.
token_risk._lookup, reused AS-IS - no on-chain logic duplicated here).

Claude (app.upstream.anthropic) writes 4 short fields from that on-chain
data ONLY: note (SAFE/CAUTION/RISKY/DANGER), a <=60-char shareable tagline,
a <=3-sentence explanation, and the disclaimer "Not financial advice." -
forced via a single tool call (app.upstream.anthropic._TOOL_SCHEMA) so the
shape is reliable without parsing free text. The system prompt forbids
inventing any fact not present in the on-chain JSON; this module can't
enforce that itself (Claude could still ignore the instruction), so the
deterministic fallback below exists for more than just timeouts - it is
also the honest baseline every Claude answer should stay close to.

Time budget
-----------
_lookup() (token_risk.py) already self-bounds to ~4.0s worst case
internally (its own _GLOBAL_DEADLINE_S) - reused unchanged, not modified,
per the instruction to reuse that analysis as-is. Composing a 2s-capped
Claude call on top of that under a <4.5s total promise leaves no safe
fixed sub-budget split (4.0 + 2.0 > 4.5), so this route wraps the WHOLE
computation (on-chain + Claude-or-fallback) in ONE outer deadline
(GLOBAL_DEADLINE_S = 4.3s, leaving ~0.2s margin under the 4.5s promise)
instead - same pattern as app/handlers/llm_gateway.py's GLOBAL_TIMEOUT_S.
If the on-chain phase alone eats most of that budget, Claude's own 2s cap
still applies on whatever remains; if the combination still overruns,
asyncio.wait_for cancels cleanly and this settles nothing (504, unpaid) -
never a partial/invented result.

Claude failure never refuses the sale
--------------------------------------
Unlike app.capacity.TokenCardCircuitBreakerMiddleware (which refuses
BEFORE the 402 when the monthly budget is exceeded or the breaker is
open - see that class's own docstring), a single Claude call failing here
(timeout, transient upstream error, malformed tool output) falls back to
the deterministic card and still settles normally - a buyer still gets a
real, honestly-labeled (card["source"]) verdict either way.
"""

from __future__ import annotations

import asyncio
import json
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app import config, db
from app.handlers.token_risk import _lookup
from app.receipts import Timer, effective_price, extract_payer_address, make_receipt, price_float
from app.upstream.evm_rpc import EvmRpcError
from app.upstream.anthropic import AnthropicCardError, generate_token_card
from app.x402_setup import ROUTE_DESCRIPTIONS, TOKEN_CARD_SAMPLE_INPUT, TOKEN_CARD_SAMPLE_OUTPUT

router = APIRouter()

GLOBAL_DEADLINE_S = 4.3
CLAUDE_TIMEOUT_S = 2.0
# 2026-10-10: the on-chain lookup phase alone has been measured eating up
# to ~2.4s of GLOBAL_DEADLINE_S (right at _lookup()'s own internal budget),
# leaving the fixed CLAUDE_TIMEOUT_S=2.0 to occasionally push the total
# past 4.3s (measured live: one real call at 4.463s). Claude's own timeout
# is now dynamic - whatever's actually left, capped at 2.0s and with a
# fixed safety margin - rather than always asking for the full 2.0s
# regardless of how much the lookup phase already spent.
CLAUDE_SAFETY_MARGIN_S = 0.2
MIN_CLAUDE_TIMEOUT_S = 0.5  # below this, skip Claude entirely - not enough time left for a real answer

_NOTES = ("SAFE", "CAUTION", "RISKY", "DANGER")


def _deterministic_card(result: dict) -> dict:
    """Mirrors app.handlers.token_risk._rules_verdict's reasoning, applied
    to the 4-way SAFE/CAUTION/RISKY/DANGER scale this route uses instead
    of token-risk's own acceptable/caution/avoid - built from the exact
    same underlying bytecode/liquidity fields, never from invented facts.
    Every sentence in the explanation is derived directly from a field
    already present in `result` - nothing is asserted that isn't backed
    by a specific flag/status in the on-chain data."""
    bytecode = result.get("bytecode_analysis") or {}
    liquidity = result.get("liquidity_analysis") or {}

    bytecode_ok = bytecode.get("status") == "ok"
    liquidity_ok = liquidity.get("status") == "ok"
    dangerous = bytecode_ok and bool(bytecode.get("any_dangerous_function"))
    renounced = bytecode_ok and bytecode.get("owner_renounced") is True
    renounced_false = bytecode_ok and bytecode.get("owner_renounced") is False
    proxy = bytecode_ok and bool(bytecode.get("is_upgradeable_proxy"))
    has_liquidity = liquidity_ok and bool(liquidity.get("any_liquidity_found"))
    active_flags = [name for name, val in (bytecode.get("flags") or {}).items() if val] if bytecode_ok else []

    if dangerous and not renounced:
        note, tagline = "DANGER", "DANGER: dangerous contract power, not renounced."
    elif (proxy and not has_liquidity) or (dangerous and not has_liquidity):
        note, tagline = "DANGER", "DANGER: risky contract shape, no liquidity found."
    elif not liquidity_ok or not has_liquidity:
        note, tagline = "RISKY", "RISKY: no on-chain liquidity confirmed."
    elif dangerous and renounced and has_liquidity:
        note, tagline = "CAUTION", "CAUTION: dangerous power present, but renounced."
    elif bytecode_ok and liquidity_ok and renounced and not proxy and has_liquidity and not dangerous:
        note, tagline = "SAFE", "Looks SAFE: renounced, liquid, no danger flags."
    else:
        note, tagline = "CAUTION", "CAUTION: mixed or incomplete on-chain signals."

    sentences = []
    if not bytecode_ok:
        sentences.append("Bytecode analysis was unavailable, so this verdict is not fully grounded.")
    else:
        if dangerous:
            sentences.append(f"This contract exposes a dangerous owner-only function ({', '.join(active_flags)}).")
        else:
            sentences.append("No dangerous owner-only function was found in the bytecode.")
        if proxy:
            sentences.append("It is an upgradeable proxy, so its logic can change after deployment.")
        elif renounced:
            sentences.append("Ownership has been renounced.")
        elif renounced_false:
            sentences.append("Ownership has not been renounced.")
    if liquidity_ok:
        sentences.append(
            "Real on-chain liquidity was found on at least one DEX."
            if has_liquidity
            else "No on-chain liquidity was found on any DEX checked."
        )
    else:
        sentences.append("Liquidity could not be checked in time.")

    return {
        "note": note,
        "tagline": tagline[:60],
        "explanation": " ".join(sentences[:3]),
        "disclaimer": "Not financial advice.",
        "source": "deterministic_fallback",
    }


async def _compute_card(body: dict) -> tuple[dict, dict, float]:
    t0 = time.monotonic()
    onchain_result = await _lookup(body)
    elapsed = time.monotonic() - t0
    claude_timeout = min(CLAUDE_TIMEOUT_S, GLOBAL_DEADLINE_S - elapsed - CLAUDE_SAFETY_MARGIN_S)

    if claude_timeout < MIN_CLAUDE_TIMEOUT_S:
        # Lookup alone already ate most of the budget - not enough left for
        # a real Claude round trip, so skip straight to the deterministic
        # card rather than attempt a call almost certain to be cancelled by
        # the outer GLOBAL_DEADLINE_S wait_for anyway (never settles either
        # way, but this avoids paying for a doomed upstream call at all).
        return onchain_result, _deterministic_card(onchain_result), 0.0

    onchain_facts = {
        "address": onchain_result["address"],
        "network": onchain_result["network"],
        "bytecode_analysis": onchain_result["bytecode_analysis"],
        "liquidity_analysis": onchain_result["liquidity_analysis"],
        "holders_analysis": onchain_result["holders_analysis"],
        "token_risk_verdict": onchain_result["verdict"],
    }
    try:
        claude_card, cost = await generate_token_card(onchain_facts, timeout_s=claude_timeout)
        card = {**claude_card, "source": "claude"}
    except AnthropicCardError as exc:
        card = _deterministic_card(onchain_result)
        cost = exc.cost_usd
    return onchain_result, card, cost


# --- GET /token-card/sample - free, static ----------------------------------
# 2026-10-10: reuses app.x402_setup.TOKEN_CARD_SAMPLE_INPUT/OUTPUT directly -
# the exact same frozen, real Claude-written card (source="claude") shown
# in the Bazaar fiche's own output example, not a second hand-written copy.
# The previous version invented a "set_max_tx_amount"/renounced-ownership
# story for real Base USDC, which has neither - wrong on both counts.

SAMPLE_REQUEST = TOKEN_CARD_SAMPLE_INPUT
SAMPLE_RESPONSE = TOKEN_CARD_SAMPLE_OUTPUT


@router.get("/token-card/sample", openapi_extra={"security": []})
async def token_card_sample():
    return {
        **SAMPLE_RESPONSE,
        "note": "Static example, not a live call.",
        "x402_receipt": make_receipt(None, "token-card", 1800, 0.0),
    }


# --- paid -------------------------------------------------------------------

async def _paid(request: Request, body: dict):
    payer = extract_payer_address(request)
    user_agent = request.headers.get("user-agent")
    body_excerpt = json.dumps(body)[:2000]

    try:
        with Timer() as timer:
            onchain_result, card, anthropic_cost = await asyncio.wait_for(
                _compute_card(body), timeout=GLOBAL_DEADLINE_S
            )
    except asyncio.TimeoutError:
        db.log_request(
            route="token-card", method=request.method, status="error", payer=payer,
            user_agent=user_agent, body_excerpt=body_excerpt, error_reason="timeout",
        )
        return JSONResponse(
            {"error": {"reason": "timeout", "detail": f"No response within {GLOBAL_DEADLINE_S:.1f}s - not charged."}},
            status_code=504,
        )
    except EvmRpcError as exc:
        reason = str(exc)
        if reason in {"invalid_address", "not_a_contract"}:
            code = 400
        elif reason == "bytecode_unavailable":
            code = 504
        else:
            code = 502
        db.log_request(
            route="token-card", method=request.method, status="error", payer=payer,
            user_agent=user_agent, body_excerpt=body_excerpt, error_reason=reason,
        )
        return JSONResponse({"error": {"reason": reason}}, status_code=code)

    price = effective_price(payer, price_float(config.PRICE_TOKEN_CARD))
    db.log_request(
        route="token-card", method=request.method, status="paid", latency_ms=timer.elapsed_ms,
        amount_usdc=price, payer=payer, user_agent=user_agent, body_excerpt=body_excerpt,
        upstream_cost_usd=anthropic_cost,
    )
    return {
        "address": onchain_result["address"],
        "network": onchain_result["network"],
        "card": card,
        "token_risk_verdict": onchain_result["verdict"],
        "x402_receipt": make_receipt(None, "token-card", timer.elapsed_ms, price),
    }


@router.get("/token-card", description=ROUTE_DESCRIPTIONS["token-card"])
async def token_card_get(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    address = (
        request.query_params.get("address")
        or request.query_params.get("token")
        or body.get("address")
        or body.get("token")
        or SAMPLE_RESPONSE["address"]
    )
    return await _paid(request, {**body, "address": address})


@router.post("/token-card", description=ROUTE_DESCRIPTIONS["token-card"])
async def token_card_post(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    return await _paid(request, body)
