"""Claude as a hidden writing engine behind specific routes (currently only
POST /token-card) - never a raw pass-through gateway like POST
/v1/chat/completions or /llm/* (see app/config.py's ANTHROPIC_API_KEY
comment). This module never logs or prints config.ANTHROPIC_API_KEY itself
- only the official anthropic SDK client reads it, once, at construction.

Budget guardrail (mandatory before any route calls generate_token_card)
--------------------------------------------------------------------------
Anthropic's API has no "get my usage" endpoint to read back, unlike
OpenRouter's /api/v1/key - so real spend is tracked ourselves: every call
this module makes returns its own real cost (input/output tokens x
config.ANTHROPIC_INPUT_PRICE_PER_MTOK/ANTHROPIC_OUTPUT_PRICE_PER_MTOK), and
the caller (app/handlers/token_card.py) logs it to requests.db via
db.log_request(upstream_cost_usd=...). monthly_budget_ok() sums that same
column for the current calendar month (db.anthropic_monthly_spend_usd()) -
"resets on the 1st" falls out of that query's date filter, no stateful
counter to reset by hand.

Circuit breaker
---------------
Any credit/billing-shaped error from Anthropic trips an in-memory breaker
for _BREAKER_COOLDOWN_S, independent of the monthly budget check - both
gates are checked by app/capacity.py's TokenCardCircuitBreakerMiddleware
BEFORE the x402 402 challenge is ever shown (same placement as every other
circuit breaker in that file), so a buyer never pays for a call that was
already known to be unavailable. _is_credit_or_billing_error()'s
classification is best-effort: built from the SDK's documented exception
hierarchy, not verified against a real depleted-credit Anthropic account
(none was available while writing this - no real ANTHROPIC_API_KEY is
configured yet, see app/config.py). Worth a manual check once the key is
live and if a real billing error is ever actually hit.
"""

from __future__ import annotations

import json
import time

import anthropic

from app import config, db

_client: "anthropic.AsyncAnthropic | None" = None


class AnthropicCardError(Exception):
    def __init__(self, reason: str, detail: str | None = None, cost_usd: float = 0.0):
        self.reason = reason
        self.detail = detail
        self.cost_usd = cost_usd  # real billed cost, if the call reached Anthropic successfully before failing downstream validation - 0.0 otherwise
        super().__init__(reason)


def _get_client() -> "anthropic.AsyncAnthropic":
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)
    return _client


# --- circuit breaker ---------------------------------------------------------

_BREAKER_COOLDOWN_S = 300.0
_breaker_tripped_until = 0.0
_breaker_last_reason: str | None = None


def breaker_tripped() -> bool:
    return time.monotonic() < _breaker_tripped_until


def breaker_status() -> dict:
    tripped = breaker_tripped()
    return {
        "tripped": tripped,
        "reason": _breaker_last_reason if tripped else None,
        "cooldown_remaining_s": round(max(0.0, _breaker_tripped_until - time.monotonic()), 1) if tripped else 0.0,
    }


def _trip_breaker(reason: str) -> None:
    global _breaker_tripped_until, _breaker_last_reason
    _breaker_tripped_until = time.monotonic() + _BREAKER_COOLDOWN_S
    _breaker_last_reason = reason


def _is_credit_or_billing_error(exc: Exception) -> bool:
    """AuthenticationError/PermissionDeniedError/RateLimitError are always
    treated as billing-adjacent - a bad/revoked/blocked key behaves
    identically to one with no credit from this route's point of view,
    both must open the breaker, not just fail one call. A BadRequestError
    whose own message mentions credit/billing/balance is Anthropic's
    documented shape for an exhausted-credit account."""
    if isinstance(exc, (anthropic.AuthenticationError, anthropic.PermissionDeniedError, anthropic.RateLimitError)):
        return True
    if isinstance(exc, anthropic.BadRequestError):
        msg = str(exc).lower()
        return any(w in msg for w in ("credit", "billing", "balance"))
    return False


# --- monthly budget -----------------------------------------------------------

def monthly_spend_usd() -> float:
    return db.anthropic_monthly_spend_usd()


def monthly_budget_ok() -> bool:
    return monthly_spend_usd() < config.ANTHROPIC_MONTHLY_BUDGET_USD


def _cost_usd(input_tokens: int, output_tokens: int) -> float:
    return (
        (input_tokens / 1_000_000) * config.ANTHROPIC_INPUT_PRICE_PER_MTOK
        + (output_tokens / 1_000_000) * config.ANTHROPIC_OUTPUT_PRICE_PER_MTOK
    )


# --- the actual call ----------------------------------------------------------

_TOOL_NAME = "emit_token_card"
_TOOL_SCHEMA = {
    "name": _TOOL_NAME,
    "description": "Emit the token verdict card, strictly grounded in the on-chain facts given in the user message.",
    "input_schema": {
        "type": "object",
        "properties": {
            "note": {"type": "string", "enum": ["SAFE", "CAUTION", "RISKY", "DANGER"]},
            "tagline": {"type": "string", "description": "Shareable one-liner, 60 characters maximum, no markdown."},
            "explanation": {"type": "string", "description": "At most 3 plain sentences, citing only the given facts."},
            "disclaimer": {"type": "string", "description": "Must be exactly 'Not financial advice.'"},
        },
        "required": ["note", "tagline", "explanation", "disclaimer"],
    },
}

_SYSTEM_PROMPT = (
    "You write a short, factual token-safety verdict card for an AI agent "
    "buyer, from ONLY the on-chain facts given in the user message (JSON). "
    "Never invent, assume, or add any fact not present in that JSON - a "
    "null or missing field means unknown, never safe and never dangerous. "
    "note must be exactly one of SAFE, CAUTION, RISKY, DANGER. tagline must "
    "be a short, shareable one-liner, 60 characters maximum, no markdown. "
    "explanation must be at most 3 plain sentences, citing only the given "
    "facts, no speculation. disclaimer must be exactly 'Not financial "
    "advice.'. Call emit_token_card with your answer - no other text."
)

_MAX_OUTPUT_TOKENS = 300


async def generate_token_card(onchain_facts: dict, timeout_s: float) -> tuple[dict, float]:
    """Returns (card_dict, cost_usd). Raises AnthropicCardError on any
    failure (not configured, breaker open, timeout, auth/billing,
    malformed tool call) - the caller (app/handlers/token_card.py) decides
    whether to fall back to the deterministic card; the breaker is tripped
    internally on a credit/billing-shaped error regardless of what the
    caller does next."""
    if not config.ANTHROPIC_API_KEY:
        raise AnthropicCardError("not_configured")
    if breaker_tripped():
        raise AnthropicCardError("breaker_open", _breaker_last_reason)

    client = _get_client()

    try:
        message = await client.messages.create(
            model=config.ANTHROPIC_MODEL,
            max_tokens=_MAX_OUTPUT_TOKENS,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": json.dumps(onchain_facts)}],
            tools=[_TOOL_SCHEMA],
            tool_choice={"type": "tool", "name": _TOOL_NAME},
            timeout=timeout_s,
        )
    except Exception as exc:
        if _is_credit_or_billing_error(exc):
            _trip_breaker(f"{type(exc).__name__}: {str(exc)[:200]}")
        reason = "timeout" if isinstance(exc, anthropic.APITimeoutError) else "upstream_error"
        raise AnthropicCardError(reason, str(exc)[:300]) from exc

    cost = _cost_usd(message.usage.input_tokens, message.usage.output_tokens)

    tool_block = next(
        (b for b in message.content if getattr(b, "type", None) == "tool_use" and b.name == _TOOL_NAME),
        None,
    )
    if tool_block is None:
        raise AnthropicCardError("malformed_response", "no tool_use block in Claude's response", cost_usd=cost)

    card = tool_block.input
    missing = [k for k in ("note", "tagline", "explanation", "disclaimer") if not card.get(k)]
    if missing or card.get("note") not in ("SAFE", "CAUTION", "RISKY", "DANGER"):
        raise AnthropicCardError(
            "malformed_response", f"missing/invalid fields: {missing}, note={card.get('note')!r}", cost_usd=cost
        )

    return card, cost
