"""POST /llm/claude-sonnet - pay-per-call anthropic/claude-sonnet-5.5, OpenAI-compatible passthrough.

Ordered provider list (2026-10-10, GET /api/v1/models/{model}/endpoints):
Anthropic, Google, Amazon Bedrock all live (100% uptime_last_30m) at the
IDENTICAL price ($2/$10 per MTok in/out) - matches _price_ceiling_usd's
own reference exactly, so margin (ceiling x MARKUP vs real cost) is
positive by construction no matter which of the 3 actually serves a
given call. allow_fallbacks=False so a request never spills onto some
other, unchecked provider.

Same engine as POST /v1/chat/completions (app/handlers/llm_gateway.py):
"exact" scheme only, ceiling computed from max_tokens via that same
pricing formula. See app/handlers/llm_per_model.py for the shared code."""

from __future__ import annotations

from app.handlers.llm_per_model import make_price_fn, make_router
from app.receipts import make_receipt

MODEL = 'anthropic/claude-sonnet-5.5'
PROVIDER = {'order': ['Anthropic', 'Google', 'Amazon Bedrock'], 'allow_fallbacks': False}
ROUTE_PATH = '/llm/claude-sonnet'
ROUTE_KEY = 'llm/claude-sonnet'

price_fn = make_price_fn(MODEL)

SAMPLE_REQUEST = {"messages": [{"role": "user", "content": "Summarize in one sentence: The Eiffel Tower is a wrought-iron lattice tower on the Champ de Mars in Paris, France. It was designed by Gustave Eiffel's engineering company and built as the entrance arch for the 1889 World's Fair. Initially criticized by some of France's leading artists and intellectuals for its design, it has become a global cultural icon of France and one of the most recognizable structures in the world, attracting millions of visitors every year."}], "max_tokens": 150}
SAMPLE_RESPONSE = {
    "id": "gen-1790780999-5D8mNBe2jJJ4gO8UyUsm",
    "object": "chat.completion",
    "created": 1790780999,
    "model": "anthropic/claude-sonnet-5.5",
    "provider": "Anthropic",
    "choices": [
        {
            "index": 0,
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": "The Eiffel Tower, a wrought-iron lattice tower in Paris designed by Gustave Eiffel's company for the 1889 World's Fair, was initially criticized by prominent French artists and intellectuals but has since become a globally recognized cultural icon that draws millions of visitors annually."},
        }
    ],
    "usage": {"prompt_tokens": 160, "completion_tokens": 100, "total_tokens": 260, "cost": 0.00132},
    "x402_receipt": make_receipt("anthropic/claude-sonnet-5.5", "llm/claude-sonnet", 1700, 0.001892),
}

router = make_router(
    route_path=ROUTE_PATH, route_key=ROUTE_KEY, model=MODEL, provider=PROVIDER,
    description_key='llm-claude-sonnet', sample_request=SAMPLE_REQUEST, sample_response=SAMPLE_RESPONSE,
)
