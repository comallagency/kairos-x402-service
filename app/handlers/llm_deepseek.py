"""POST /llm/deepseek - pay-per-call deepseek/deepseek-v4-pro, OpenAI-compatible passthrough.

Re-pinned 2026-10-10 (still DEEPSEEK_ENABLED=False, not live either way -
see below): the previous pin (Reka) was a real margin leak, found during
a full LLM-provider audit, not a latency incident - GET /api/v1/models/
{model}/endpoints prices Reka at $1.05/$10.50 per MTok in/out, while
_price_ceiling_usd's reference for this model is $0.9483/$1.8966 per
MTok (StreamLake's price) - charging the ceiling (reference x 1.10
MARKUP) while actually paying Reka would have LOST money on every real
completion token (margin = ref*1.10 - real = -8.32e-6/token). Ordered
list now: StreamLake (exact reference match, 100% uptime_last_30m),
GMICloud (slightly above reference but still margin-positive once the
1.10 markup is applied: +8.6e-8/prompt-token, +1.72e-7/completion-token).
Reka and every other live candidate (DigitalOcean, Cloudflare, DeepInfra,
...) priced above what the 10% markup can cover were excluded on that
basis alone, not reliability.

Same engine as POST /v1/chat/completions (app/handlers/llm_gateway.py):
"exact" scheme only, ceiling computed from max_tokens via that same
pricing formula. See app/handlers/llm_per_model.py for the shared code."""

from __future__ import annotations

from app.handlers.llm_per_model import make_price_fn, make_router
from app.receipts import make_receipt

# Withdrawn from the catalog 2026-09-30 (real p95 measured at 20014ms (2/10 hit the 20s ceiling) on
# 10 real calls, after adding the 8s/20s provider-fallback timeout fix
# the same day) - see app/x402_setup.py's _core_route_configs() for
# where this excludes the route, and app/mcp_server.py for the MCP tool.
# Reintegrate only once a real measurement (different provider and/or
# model version) clears >=29/30 on 30 real calls - not attempted yet.
DEEPSEEK_ENABLED = False

MODEL = 'deepseek/deepseek-v4-pro'
PROVIDER = {'order': ['StreamLake', 'GMICloud'], 'allow_fallbacks': False}
ROUTE_PATH = '/llm/deepseek'
ROUTE_KEY = 'llm/deepseek'

price_fn = make_price_fn(MODEL)

SAMPLE_REQUEST = {"messages": [{"role": "user", "content": "Summarize in one sentence: The Eiffel Tower is a wrought-iron lattice tower on the Champ de Mars in Paris, France. It was designed by Gustave Eiffel's engineering company and built as the entrance arch for the 1889 World's Fair. Initially criticized by some of France's leading artists and intellectuals for its design, it has become a global cultural icon of France and one of the most recognizable structures in the world, attracting millions of visitors every year."}], "max_tokens": 150}
SAMPLE_RESPONSE = {
    "id": "gen-1790781087-DvBLYQhDzogYW0WpOA5r",
    "object": "chat.completion",
    "created": 1790781087,
    "model": "deepseek/deepseek-v4-pro",
    "provider": "StreamLake",
    "choices": [
        {
            "index": 0,
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": "Despite initial criticism, the Eiffel Tower, designed by Gustave Eiffel's company for the 1889 World's Fair, has evolved into a globally recognized cultural icon of France that attracts millions of visitors annually."},
        }
    ],
    "usage": {"prompt_tokens": 110, "completion_tokens": 46, "total_tokens": 156, "cost": 0.0002626},
    "x402_receipt": make_receipt("deepseek/deepseek-v4-pro", "llm/deepseek", 1700, 0.001),
}

router = make_router(
    route_path=ROUTE_PATH, route_key=ROUTE_KEY, model=MODEL, provider=PROVIDER,
    description_key='llm-deepseek', sample_request=SAMPLE_REQUEST, sample_response=SAMPLE_RESPONSE,
)
