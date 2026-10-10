import logging
import time

from cdp.x402 import create_facilitator_config
from x402 import SettleContext, SettleResponse, SkipSettleResult
from x402.mechanisms.evm.exact.register import register_exact_evm_server
from x402.extensions.bazaar import (
    OutputConfig,
    bazaar_resource_server_extension,
    declare_discovery_extension,
)
from x402.http.facilitator_client import FacilitatorConfig, HTTPFacilitatorClient
from x402.http.middleware.fastapi import PaymentMiddlewareASGI
from x402.http.types import PaymentOption, RouteConfig
from x402.server import x402ResourceServer

from app import config, db
from app.receipts import extract_payer_from_payment_dict

logger = logging.getLogger("x402.setup")

# Captured from a real call (2026-09-30, no payment) - two distinct
# sources (wikipedia, hackernews), matching what Jev routing actually
# returns for a general/news-leaning query, not a single-source placeholder.
SEARCH_SAMPLE_OUTPUT = {
    "query": "recent earthquake news",
    "results": [
        {
            "title": "Lists of earthquakes",
            "url": "https://en.wikipedia.org/wiki/Lists_of_earthquakes",
            "date": None,
            "source": "wikipedia",
            "extract": "to Earthquakes. USGS-ANSS Latest earthquakes around the world Southern California Earthquake Center (SCEC) IRIS Seismic Monitor, Recent earthquakes around",
        },
        {
            "title": "List of earthquakes in 2026",
            "url": "https://en.wikipedia.org/wiki/List_of_earthquakes_in_2026",
            "date": None,
            "source": "wikipedia",
            "extract": "This is a list of earthquakes in 2026. Only earthquakes of magnitude 6 or above are included, unless they result in significant damage and/or casualties",
        },
        {
            "title": "Is Recent Earthquake Activity Unusual? Scientists Say No.",
            "url": "http://www.usgs.gov/newsroom/article.asp?ID=2439",
            "date": "2010-04-15T00:49:30Z",
            "source": "hackernews",
            "extract": "Is Recent Earthquake Activity Unusual? Scientists Say No.",
        },
        {
            "title": "2026 Venezuela earthquakes",
            "url": "https://en.wikipedia.org/wiki/2026_Venezuela_earthquakes",
            "date": None,
            "source": "wikipedia",
            "extract": "doublet large strike-slip earthquakes affected northwestern and central Venezuela. The epicenter of the first earthquake was in Veroes Municipality,",
        },
        {
            "title": "Lists of 21st-century earthquakes",
            "url": "https://en.wikipedia.org/wiki/Lists_of_21st-century_earthquakes",
            "date": None,
            "source": "wikipedia",
            "extract": "tsunami is one of the deadliest natural disasters in recent history. The 2005 Kashmir earthquake destroyed several towns, and caused extensive damage",
        },
    ],
}

TRANSLATE_SAMPLE_OUTPUT = {
    "text": "Bonjour le monde",
    "target_lang": "en",
    "detected_source_lang": "fr",
    "translated_text": "Hello world",
    "model_served": "agentindex-translate-1",
}

JOBS_SAMPLE_OUTPUT = {
    "job_id": "sample-0000",
    "status": "done",
    "result": {
        "subject": "Example Corp",
        "summary": "Example Corp is a fictitious company used for demonstration purposes.",
        "sources": ["https://example.com/about"],
    },
}

PDF_SAMPLE_OUTPUT = {
    "markdown": (
        "# Universal Declaration of Human Rights \n\n## Preamble \n\nWhereas "
        "recognition of the inherent dignity and of the equal and inalienable "
        "rights of all members of the human family is the foundation of "
        "freedom, justice and peace in the world, ..."
    ),
    "metadata": {"title": None, "author": "lindner", "pages": 8, "date": "2001-09-10T01:04:58-07:00"},
    "token_count": 2164,
}

WEB_READ_SAMPLE_OUTPUT = {
    "url": "https://www.w3.org/",
    "title": "World Wide Web Consortium (W3C)",
    "markdown": "# World Wide Web Consortium (W3C)\n\nThe W3C mission is to lead the web to its full potential.",
    "token_count": 154,
}

EXTRACT_SAMPLE_OUTPUT = {
    "data": {"product_name": "Widget Pro", "price": 29.99, "in_stock": True},
    "missing_fields": [],
}

SUMMARIZE_SAMPLE_OUTPUT = {
    "summary": "The article explains how photosynthesis converts light energy into chemical energy in plants.",
    "length": "short",
    "sources": ["https://en.wikipedia.org/wiki/Photosynthesis"],
}

FACT_CHECK_SAMPLE_OUTPUT = {
    "claim": "The Eiffel Tower is taller than the Statue of Liberty.",
    "verdict": "supported",
    "confidence": 0.92,
    "sources": [{"url": "https://example.com/eiffel-tower-facts", "title": "Eiffel Tower Facts", "stance": "supports"}],
}

WEATHER_SAMPLE_OUTPUT = {
    "query": "Paris",
    "location": {
        "name": "Paris",
        "country": "France",
        "country_code": "FR",
        "latitude": 48.85341,
        "longitude": 2.3488,
        "timezone": "Europe/Paris",
    },
    "current": {
        "time": "2026-09-30T12:30",
        "temperature_c": 21.1,
        "humidity_pct": 78,
        "wind_speed_kmh": 8.9,
        "weather_code": 61,
        "conditions": "slight_rain",
    },
    "daily": [
        {
            "date": "2026-09-30",
            "temperature_max_c": 22.8,
            "temperature_min_c": 20.5,
            "precipitation_sum_mm": 13.2,
            "weather_code": 63,
            "conditions": "rain",
        },
        {
            "date": "2026-10-01",
            "temperature_max_c": 21.0,
            "temperature_min_c": 16.0,
            "precipitation_sum_mm": 1.9,
            "weather_code": 80,
            "conditions": "rain_showers",
        },
        {
            "date": "2026-10-02",
            "temperature_max_c": 20.6,
            "temperature_min_c": 12.5,
            "precipitation_sum_mm": 0.0,
            "weather_code": 3,
            "conditions": "overcast",
        },
    ],
}

CRYPTO_SAMPLE_OUTPUT = {
    "vs_currency": "usd",
    "prices": [
        {"id": "bitcoin", "symbol": "btc", "price": 81218.0, "change_24h_pct": -1.2},
        {"id": "ethereum", "symbol": "eth", "price": 2634.1, "change_24h_pct": 0.4},
    ],
}

NEWS_SAMPLE_OUTPUT = {
    "source": "hacker-news",
    "count": 1,
    "stories": [
        {
            "id": 49765348,
            "title": "Example headline about AI agents",
            "url": "https://example.com/story",
            "score": 312,
            "by": "pg",
            "time": 1726750000,
            "comments": 88,
        }
    ],
}

CAN_PAY_SAMPLE_OUTPUT = {
    "address": "0xb3F32bdfe8D07825BC0D7387295aB1D7559BA69d",
    "network": "eip155:8453",
    "usdc": {"balance": 0.0, "balance_atomic": "0", "contract": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"},
    "eth": {"balance": 0.0},
    "amount_requested": 0.001,
    "can_pay": False,
    "shortfall_usdc": 0.001,
}

PROBE_SAMPLE_OUTPUT = {
    "url": "https://x402.shizu.me/weather?lat=48.85&lon=2.35",
    "reachable": True,
    "http_status": 402,
    "is_x402": True,
    "x402_version": 1,
    "price_usdc": 0.004,
    "network": "base",
    "pay_to": "0x217e5Fe265EB78b29067bF8324ef03a7D8e167C4",
    "scheme": "exact",
}

WALLET_BALANCE_SAMPLE_OUTPUT = {
    "address": "0xb3F32bdfe8D07825BC0D7387295aB1D7559BA69d",
    "network": {"key": "base", "name": "Base", "caip2": "eip155:8453"},
    "block_number": 51500000,
    "native": {"symbol": "ETH", "balance": 0.001, "balance_wei": "1000000000000000"},
    "usdc": {
        "balance": 1.25,
        "balance_atomic": "1250000",
        "decimals": 6,
        "contract": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    },
}

GAS_PRICE_SAMPLE_OUTPUT = {
    "network": {"key": "base", "name": "Base", "caip2": "eip155:8453"},
    "block_number": 51500000,
    "gas_price": {"wei": "1000000", "gwei": 0.001},
    "base_fee": {"wei": "900000", "gwei": 0.0009},
    "native_transfer_estimate": {
        "gas_limit": 21000,
        "fee_wei": "21000000000",
        "fee_native": 0.000000021,
        "symbol": "ETH",
    },
}

WALLET_INTELLIGENCE_SAMPLE_OUTPUT = {
    "address": "0xb3F32bdfe8D07825BC0D7387295aB1D7559BA69d",
    "requested_networks": ["base", "ethereum", "polygon", "arbitrum", "optimism"],
    "successful_networks": 5,
    "rpc_reads_replaced": 25,
    "x402_readiness": {
        "network": "base", "amount_usdc": 0.001, "balance_usdc": 1.25,
        "can_pay": True, "shortfall_usdc": 0.0,
    },
    "networks": [
        {
            "network": {"key": "base", "name": "Base", "caip2": "eip155:8453"},
            "block_number": 51500000,
            "native": {"symbol": "ETH", "balance": 0.001},
            "usdc": {"balance": 1.25, "balance_atomic": "1250000"},
            "gas_price": {"wei": "1000000", "gwei": 0.001},
            "native_transfer_estimate": {"fee_native": 0.000000021, "symbol": "ETH"},
        }
    ],
    "errors": [],
}

X402_ECHO_SAMPLE_OUTPUT = {
    "ok": True,
    "purpose": "x402-mainnet-conformance",
    "network": "eip155:8453",
    "price_usdc": 0.001,
    "price_atomic_usdc": "1000",
    "payer": "0xb3F32bdfe8D07825BC0D7387295aB1D7559BA69d",
    "request": {"method": "POST", "echo": {"hello": "agent"}},
}

AGENT_HEALTH_SAMPLE_OUTPUT = {
    "url": "https://x402.agentindex.world/search",
    "origin": "https://x402.agentindex.world",
    "operational": True,
    "verdict": "operational",
    "score": 100,
    "latency_ms": 84,
    "target": {"reachable": True, "http_status": 402},
    "x402": {
        "valid": True, "version": 2, "price_usdc": 0.0001,
        "network": "eip155:8453",
    },
    "discovery": {
        "openapi": {"found": True, "status": 200, "path_count": 18},
        "x402_manifest": {"found": True, "status": 200},
        "agent_card": {"found": True, "status": 200, "skill_count": 27},
        "mcp_manifest": {"found": True, "status": 200},
    },
    "issues": [],
}

# Common thread across the content kit, both in tags (below) and spelled out in
# prose in each description - an agent that finds one of these should
# understand there are others for the same job (see GET /capabilities).
KIT_TAGS = ["web", "read", "extract", "markdown", "llm-ready"]
# Bazaar iconUrl: a real 256px PNG (app/static/icon.png, served at
# /icon.png) - absolute http(s) URL, well under the SDK's 2048-char cap.
ICON_URL = f"{config.BASE_URL}/icon.png"

# Single source of truth for "what is this origin" - used in app/main.py's
# FastAPI info.description, the MCP server's instructions, and
# GET /capabilities. Before 2026-09-06 these three had each drifted to a
# different, some stale, description ("web search, translation and research
# jobs" - the OLD 3-route identity, long after the 9-route kit shipped) -
# exactly the kind of drift external catalogs cache and never refresh on
# their own.
#
# Changed again 2026-09-07: /search and /fact-check are disabled (see
# _core_route_configs() below) because both depend on OpenRouter's paid
# "web" plugin (app/upstream/websearch.py) and the account's credit balance
# went negative - every call was charging a buyer and returning a 502. The
# kit's identity shifts from "read and search the web" to "process what the
# agent hands you" (pdf, web-read, extract, summarize, detect-language) -
# every remaining route runs on content the caller supplies (a file, a URL,
# raw text/HTML) plus, at most, a $0 completion on a free-tier model - no
# paid upstream call is on any surviving path. Re-enable by restoring the two
# route entries below once a real OpenRouter balance exists again.
KIT_TAGLINE = (
    "Pay-per-call tools for AI agents: web search + content, documents, live "
    "market data, and multi-chain wallet/gas reads. Mainnet test from "
    "$0.000001 USDC on Base."
)
_KIT_MENTION = (
    "Part of the AgentIndex content kit (pdf, web-read, extract, summarize, "
    "detect-language) - see GET /capabilities."
)


# --- POST /discover (paid, semantic search over Kairos's local MCP snapshot) ---

DISCOVER_INPUT_SCHEMA = {
    "properties": {
        "q": {
            "type": "string",
            "description": (
                "The need to match, in plain language - e.g. \"read a PDF and "
                "give me markdown\" or \"persistent knowledge graph\". Matched "
                "against a curated snapshot of MCP servers (official registry + "
                "carnet) by semantic similarity."
            ),
        },
        "max_results": {
            "type": "integer", "minimum": 1, "maximum": 25,
            "description": "Maximum number of servers to return (default 5).",
        },
        "min_similarity": {
            "type": "number", "minimum": 0.0, "maximum": 1.0,
            "description": (
                "Minimum cosine similarity to include a result (default 0.30). "
                "Raise it for fewer, closer matches."
            ),
        },
    },
    "required": ["q"],
}

DISCOVER_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "q": {"description": "The query that was matched."},
        "snapshot_date": {
            "type": "string",
            "description": "Date the underlying MCP snapshot was taken.",
        },
        "snapshot_rows": {
            "type": "integer",
            "description": "Number of MCP servers in the snapshot.",
        },
        "min_similarity": {
            "type": "number",
            "description": "The similarity threshold applied.",
        },
        "matches": {
            "type": "integer",
            "description": "Number of servers above the threshold before the top-N cut.",
        },
        "results": {
            "type": "array",
            "description": "Best-matching MCP servers, most relevant first.",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Server name as observed."},
                    "url": {
                        "description": "Endpoint URL, when one was observed.",
                    },
                    "description": {
                        "type": "string",
                        "description": "What the server does, as observed.",
                    },
                    "registry": {
                        "description": "Registry or listing where it was seen.",
                    },
                    "relevance": {
                        "type": "number",
                        "description": "Cosine similarity between the query and this server, 0 to 1.",
                    },
                },
            },
        },
    },
    "required": ["q", "results"],
}

DISCOVER_INPUT_EXAMPLE = {
    "q": "read a PDF and give me markdown",
    "max_results": 5,
    "min_similarity": 0.3,
}

DISCOVER_SAMPLE_OUTPUT = {
    "q": "read a PDF and give me markdown",
    "snapshot_date": "2026-09-18",
    "snapshot_rows": 10327,  # illustrative only - the real response always computes this live (len(snapshot))
    "min_similarity": 0.3,
    "matches": 3,
    "results": [
        {
            "name": "PDF Extract MCP",
            "url": "https://example.org/mcp/",
            "description": "Extract a public PDF into clean markdown text, plus metadata and a real token count.",
            "registry": "registre-mcp",
            "relevance": 0.7211,
            "observed_at": "2026-09-02T19:07:49.374328Z",
        },
        {
            "name": "Docling Server",
            "url": None,
            "description": "Document conversion to markdown and structured text for LLM agents.",
            "registry": "annuaire",
            "relevance": 0.6487,
            "observed_at": "2026-09-10T12:00:00Z",
        },
        {
            "name": "Srclight",
            "url": "https://example.com/mcp/",
            "description": "Deep code indexing for AI agents. FTS5 + embeddings + call graphs. Fully local.",
            "registry": "registre-mcp",
            "relevance": 0.4102,
            "observed_at": "2026-09-15T08:30:00Z",
        },
    ],
}

ROUTE_DESCRIPTIONS = {
    "search": (
        "Jev-routed web search: each query goes to the best specialised "
        "source (code, facts, news, prices), results ranked by relevance. "
        "28/30 test queries answered. Up to 10 results per query, each with "
        "title, URL, snippet, short extract and publish date. Up to 5 "
        "queries per call, merged and de-duplicated. No account, no API "
        "key. Try GET /search/sample. "
        + _KIT_MENTION
    ),
    "translate": (
        "Translate up to 200 text segments in a single call - the same result "
        "that would otherwise take 200 separate calls to translate a whole "
        "file. Markdown, HTML and {x} placeholders preserved per segment, "
        "order and count kept intact, source language auto-detected, "
        "automatic fallback across multiple models for uptime, providers "
        "that train on submitted prompts excluded. A single string also "
        "works. No account, no API key. Try GET /translate/sample."
    ),
    "jobs": (
        "Multi-query web research, read and synthesized into one sourced JSON "
        "brief in a single call - what would otherwise cost an agent twenty calls "
        "and its whole context window. Free status polling and result retrieval. "
        "Try GET /jobs/sample."
    ),
    "pdf": (
        "Extract a public PDF (by HTTPS URL or base64) into clean Markdown - "
        "headings and tables preserved via layout-aware parsing, plus title/author/"
        "page count/date metadata and a real token count. Text content itself is "
        "never generated or altered, only its structure is inferred. Try GET "
        "/pdf/sample. "
        + _KIT_MENTION
    ),
    "web-read": (
        "Fetch URL content as Markdown: read any web page and get clean "
        "Markdown - navigation, ads and boilerplate stripped, links resolved, "
        "plus a real token count. The same extraction /search uses on result "
        "pages, exposed standalone for a URL you already have. Try GET "
        "/web-read/sample. " + _KIT_MENTION
    ),
    "extract": (
        "Extract structured data from a URL or raw text into strict JSON matching "
        "a schema you provide. Fields the content doesn't support come back null - "
        "never an invented or approximate value, and an unmatchable schema returns "
        "an explicit error. Try GET /extract/sample. " + _KIT_MENTION
    ),
    "summarize": (
        "Summarize a URL, raw text or HTML at the length you request - short, "
        "medium or long - with the source noted. Neutral and factual, no invented "
        "facts. Try GET /summarize/sample. " + _KIT_MENTION
    ),
    "fact-check": (
        "Check a claim against live web sources - a verdict, a confidence level, "
        "and the sources that support or contradict it, each with its URL and "
        "stance. Returns 'inconclusive' rather than a guess when the sources "
        "don't clearly settle it. Try GET /fact-check/sample. " + _KIT_MENTION
    ),
    "discover": (
        "Find MCP servers matching a need, ranked by semantic similarity over a "
        "curated snapshot of MCP servers. Returns name, endpoint, "
        "description, source registry and a 0-1 relevance per match. Paid POST: "
        "up to 25 matches. Try GET /discover/sample."
    ),
    "weather": (
        "Current weather and a 3-day forecast for any city or lat/lon - "
        "temperature, humidity, wind, conditions. No account, no API key. "
        "Try GET /weather/sample."
    ),
    "crypto": (
        "Live spot prices for crypto coins (BTC, ETH, SOL, ...) in USD or another "
        "fiat, with 24h change. Accepts tickers or CoinGecko ids. Try GET "
        "/crypto/sample."
    ),
    "news": (
        "Top Hacker News headlines right now - title, URL, score, comments. "
        "Fresh links for agents that need current tech/startup signal. Try GET "
        "/news/sample."
    ),
    "can-pay": (
        "Check whether a Base wallet holds enough USDC to pay an x402 amount "
        "(default $0.001). Returns balance, can_pay, shortfall. The checklist "
        "agents run before spending. Try GET /can-pay/sample."
    ),
    "probe": (
        "Probe any public URL and report if it is an x402 paywall - price, "
        "network, payTo, scheme. Shop paywalls before you spend. Try GET "
        "/probe/sample."
    ),
    "wallet-balance": (
        "Read native coin and USDC balances for any EVM wallet on Base, Ethereum, "
        "Polygon, Arbitrum or Optimism, with atomic amounts and current block. "
        "No RPC key or account. Try GET /wallet-balance/sample."
    ),
    "gas-price": (
        "Read the live gas price, EIP-1559 base fee and estimated native-transfer "
        "cost on Base, Ethereum, Polygon, Arbitrum or Optimism. No RPC key or "
        "account. Try GET /gas-price/sample."
    ),
    "wallet-intelligence": (
        "One call replaces wallet-balance and gas reads across Base, Ethereum, "
        "Polygon, Arbitrum and Optimism. Returns native and USDC balances, current "
        "blocks, fees and Base x402 payment readiness. One signature instead of "
        "ten paid requests. Try GET /wallet-intelligence/sample."
    ),
    "x402-echo": (
        "Cheapest possible Base-mainnet x402 conformance call: settle $0.001 "
        "of USDC and receive payer identity, request "
        "echo and receipt. Built for agents testing wallets, Payment-Signature, "
        "facilitators and settlement. Try GET /x402-echo/sample."
    ),
    "tip": (
        "Voluntarily support AgentIndex with one cent of Base USDC. Returns payer "
        "identity, a thank-you acknowledgement and an x402 settlement receipt. "
        "Use this when an agent wants to sponsor open pay-per-call infrastructure."
    ),
    "agent-claim": (
        "Buy a 30-day verified agent listing for $0.01 USDC. AgentIndex audits "
        "reachability, x402 compatibility and discovery manifests, then publishes "
        "a machine-readable registry entry and score badge for buyer trust and SEO."
    ),
    "agent-health": (
        "Fresh operational audit of any public agent or paid API: reachability, "
        "latency, HTTP status, x402 challenge, price/network/payTo, OpenAPI, "
        "agent card and MCP manifest. Returns a 0-100 score, verdict and issues. "
        "Try GET /agent-health/sample."
    ),
    "decide": (
        "Ask up to 20 typed yes/no, multiple-choice or ordinal-scale questions "
        "about one shared piece of context in a single call - each answered "
        "with a real probability (Jev decision primitives), not generated "
        "text. Powered by Jev (TypeSafe System One). $0.001 per call, no "
        "OpenRouter or TypeSafe account needed. Try GET /decide/sample."
    ),
    "guard": (
        "Ask whether a tool call should run automatically, need human "
        "confirmation, or be denied, given the user's request - returns "
        "allow/ask/deny with a probability. Advisory only: like any "
        "LLM-based judge it is sensitive to prompt injection in the request "
        "or tool call, so the calling agent must keep the final decision, "
        "not delegate it outright. Powered by Jev (TypeSafe System One). "
        "$0.001 per call, no OpenRouter or TypeSafe account needed. Try GET "
        "/guard/sample."
    ),
    "verify": (
        "Check whether a source supports, contradicts, or gives insufficient "
        "information about a claim - a typed verdict with a real probability, "
        "not generated text. Powered by Jev (TypeSafe System One). $0.001 per "
        "call, no OpenRouter or TypeSafe account needed. Try GET /verify/sample."
    ),
    "rank": (
        "Rank up to 50 documents by relevance to a query in a single call - "
        "each document returned with its relevance probability, sorted "
        "highest first. Powered by Jev (TypeSafe System One). $0.001 per call, "
        "no OpenRouter or TypeSafe account needed. Try GET /rank/sample."
    ),
    "llm-gateway": (
        "437 models, no account, no API key, pay per call in USDC - "
        "OpenAI-compatible chat completions and AI inference gateway "
        "(Claude, GPT, Gemini, Llama, Mistral and more in one endpoint). "
        "Price: from $0.001, computed from your max_tokens × model rate "
        "× 1.10 (you sign a ceiling; unused tokens are not refunded). "
        "20s server-side timeout - never charged if it fires; set your "
        "client timeout to 30s. Try GET /v1/chat/completions/sample."
    ),
    "token-risk": (
        "Base token risk scan: on-chain rug check and honeypot-style flag "
        "detection for any ERC-20, no GoPlus, DexScreener or other vendor - "
        "real Base token safety. Reads bytecode for mint/blacklist/pause/tax "
        "powers, EIP-1967 proxy, renounced ownership, plus Uniswap v2/v3 and "
        "Aerodrome liquidity. A Jev avoid/caution/acceptable verdict leads "
        "the response, raw signals follow. Holder concentration included "
        "only when history fits 1-2 log queries. Try GET /token-risk/sample."
    ),
    "token-card": (
        "Base token verdict card written by Claude Haiku 5.5 (Anthropic) "
        "from real on-chain data: SAFE/CAUTION/RISKY/DANGER, a tagline, a "
        "short explanation. A deterministic fallback takes over if Claude "
        "is slow, honestly labeled. $0.005 - about 4x cheaper than "
        "comparable cards. Try GET /token-card/sample."
    ),
    "research": (
        "Jev-powered research: routed search, cited answer, claims "
        "verified against sources. Ask a question, get a 5-8 sentence "
        "answer, each claim tagged to a numbered source, built from real "
        "page content - no invented facts. A fast paid LLM writes it, "
        "under 4.5s guaranteed; on a rare miss, a no-LLM extractive "
        "fallback (top sentences from the sources, picked by Jev) takes "
        "over instead, marked \"synthesis\":\"extractive\". Try GET "
        "/research/sample."
    ),
    "sentiment": (
        "Sentiment analysis: classify text as positive, negative, or neutral "
        "with a confidence score and an alternate label. Powered by Jev, "
        "with an automatic Groq-hosted LLM fallback if Jev is slow or "
        "unavailable (\"engine\":\"jev\"|\"fallback\" in the response) - under "
        "4.5s guaranteed. Try GET /sentiment/sample."
    ),
    "classify": (
        "Text classification with your own custom labels (2-20) - "
        "categorize a support ticket, a review, or any text into the "
        "best-fitting label, with a confidence score and an alternate "
        "label. Powered by Jev, falling back to a Groq-hosted LLM if Jev "
        "is slow or unavailable - under 4.5s guaranteed. Try GET "
        "/classify/sample."
    ),
    "intent": (
        "Intent detection: classify text as a question, request, complaint, "
        "compliment, or other, with a confidence score and an alternate "
        "label. Powered by Jev, with an automatic Groq-hosted LLM fallback "
        "if Jev is slow or unavailable - under 4.5s guaranteed. Try GET "
        "/intent/sample."
    ),
    "spam-check": (
        "Spam detection: classify text as spam or not spam with a "
        "confidence score. Powered by Jev, with an automatic Groq-hosted "
        "LLM fallback if Jev is slow or unavailable - under 4.5s "
        "guaranteed. Try GET /spam-check/sample."
    ),
    "toxicity": (
        "Toxicity detection: classify text as toxic or not toxic (hate "
        "speech, harassment, threats, severe offensive language) with a "
        "confidence score. Powered by Jev, with an automatic Groq-hosted "
        "LLM fallback if Jev is slow or unavailable - under 4.5s "
        "guaranteed. Try GET /toxicity/sample."
    ),
    "language": (
        "Language detection: identify which of 20 common languages (or "
        "\"other\") a text is written in, with a confidence score. Powered "
        "by Jev, with an automatic Groq-hosted LLM fallback if Jev is slow "
        "or unavailable - under 4.5s guaranteed. Try GET /language/sample."
    ),
    "pii-check": (
        "PII detection: flag whether text contains personally identifiable "
        "information (name plus contact details, email, phone, address, "
        "government ID, financial account) with a confidence score. "
        "Powered by Jev, with an automatic Groq-hosted LLM fallback if Jev "
        "is slow or unavailable - under 4.5s guaranteed. Try GET "
        "/pii-check/sample."
    ),
    "llm-claude-sonnet": (
        "Claude Sonnet API - pay per call, no API key. OpenAI-format chat "
        "completions on anthropic/claude-sonnet-5.5, pinned to Anthropic's "
        "own OpenRouter endpoint for reliability. You sign a fixed price "
        "computed from your max_tokens; unused tokens are not refunded. "
        "20s server-side timeout - never charged if it fires; set your "
        "client timeout to 30s. Try GET /llm/claude-sonnet/sample."
    ),
    "llm-gpt-mini": (
        "GPT Mini API - pay per call, no API key. OpenAI-format chat "
        "completions on openai/gpt-5.4-mini, pinned to OpenAI's own "
        "OpenRouter endpoint for reliability. You sign a fixed price "
        "computed from your max_tokens; unused tokens are not refunded. "
        "20s server-side timeout with an automatic 2nd-provider retry if "
        "the first is slow - never charged if both miss; set your client "
        "timeout to 30s. Try GET /llm/gpt-mini/sample."
    ),
    "llm-gemini-flash": (
        "Gemini Flash API - pay per call, no API key. Chat completions on "
        "google/gemini-3.8-flash, pinned to Google AI Studio. Spends "
        "hidden reasoning tokens even on simple prompts - pass "
        "max_tokens=500+ or you may get an empty/truncated response. "
        "Fixed price from your max_tokens, unused tokens not refunded. "
        "20s timeout with a 2nd-provider retry if the first is slow - "
        "never charged if both miss; set your client timeout to 30s. "
        "Try GET /llm/gemini-flash/sample."
    ),
    "llm-llama": (
        "Llama API - pay per call, no API key. OpenAI-format chat "
        "completions on meta-llama/llama-4-maverick, pinned to DeepInfra "
        "for reliability. You sign a fixed price computed from your "
        "max_tokens; unused tokens are not refunded. 20s server-side "
        "timeout - never charged if it fires; set your client timeout to "
        "30s. Try GET "
        "/llm/llama/sample."
    ),
    "llm-deepseek": (
        "DeepSeek API - pay per call, no API key. OpenAI-format chat "
        "completions on deepseek/deepseek-v4-pro, pinned to Reka for "
        "reliability (the fastest of 3 measured providers). You sign a "
        "fixed price computed from your max_tokens; unused tokens are not "
        "refunded. 20s server-side timeout with an automatic 2nd-provider "
        "retry if the first is slow - never charged if both miss; set "
        "your client timeout to 30s. Try GET /llm/deepseek/sample."
    ),
}

# Non-vital startup check: a description that grew past the Bazaar limit is
# a bug worth flagging loudly, but it must never be the reason the whole
# service fails to boot (a crash-loop from this exact assert took /search
# down in production on 2026-09-27). Truncate and warn instead.
_DESCRIPTION_MAX_CHARS = 500
for _name, _desc in ROUTE_DESCRIPTIONS.items():
    if len(_desc) > _DESCRIPTION_MAX_CHARS:
        logger.warning(
            "ROUTE_DESCRIPTIONS[%r] is %d chars (max %d) - truncating instead of "
            "crashing the service; fix the source text when convenient.",
            _name, len(_desc), _DESCRIPTION_MAX_CHARS,
        )
        ROUTE_DESCRIPTIONS[_name] = _desc[: _DESCRIPTION_MAX_CHARS - 1].rstrip() + "\u2026"

# Single source of truth for each route's input shape - consumed by the Bazaar
# discovery extension below, by app/openapi_custom.py for the standard OpenAPI
# requestBody (AgentCash discovery expects one), and by app/mcp_server.py's
# MCP tool schemas. Duplicating these per consumer is exactly the class of
# drift bug that once made the Bazaar tags go stale (see BRIEF-CORRECTIONS.md).
SEARCH_INPUT_SCHEMA = {
    "properties": {
        "query": {
            "description": (
                "Search query, or an array of up to 5 related queries to run in "
                "one call - results are merged and de-duplicated by URL."
            ),
            "oneOf": [
                {"type": "string"},
                {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 5,
                },
            ],
        },
        "max_results": {
            "type": "integer", "minimum": 1, "maximum": 10,
            "description": "Maximum number of results to return.",
        },
        "extract": {
            "type": "boolean",
            "description": "Include the search-engine snippet for each result.",
        },
        "include_content": {
            "type": "boolean",
            "description": "Fetch result pages and include clean full-page Markdown. Defaults to true.",
        },
        "content_results": {
            "type": "integer", "minimum": 1, "maximum": 3,
            "description": "How many top result pages to fetch when include_content is true.",
        },
        "content_chars": {
            "type": "integer", "minimum": 1000, "maximum": 20000,
            "description": "Maximum Markdown characters returned per fetched result page.",
        },
        "summarize": {
            "type": "boolean",
            "description": "Also return a short synthesized answer summarizing the results.",
        },
    },
    "required": ["query"],
}

TRANSLATE_INPUT_SCHEMA = {
    "properties": {
        "text": {
            "description": (
                "Text to translate - a single string, or an array of up to 200 "
                "segments to translate together in one call (batch/bulk "
                "translation), preserving order and count."
            ),
            "oneOf": [
                {"type": "string"},
                {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 200,
                },
            ],
        },
        "target_lang": {
            "type": "string",
            "description": "Target language as an ISO 639-1 code, e.g. 'en', 'fr', 'ja'.",
        },
        "source_lang": {
            "type": "string",
            "description": "Source language code. Omit to auto-detect.",
        },
        "preserve_format": {
            "type": "boolean",
            "description": (
                "Keep markdown syntax, HTML tags and {x}/{{x}} placeholders "
                "unchanged, translating only the surrounding natural-language "
                "text - useful for localizing UI strings or templated content."
            ),
        },
    },
    "required": ["text", "target_lang"],
}

JOBS_INPUT_SCHEMA = {
    "properties": {
        "subject": {
            "type": "string",
            "description": "Subject or question to research - a company, person, product, claim or topic.",
        },
    },
    "required": ["subject"],
}

PDF_INPUT_SCHEMA = {
    "properties": {
        "url": {
            "type": "string",
            "description": "Public HTTPS URL of a PDF file. Provide this or pdf_base64, not both.",
        },
        "pdf_base64": {
            "type": "string",
            "description": "Base64-encoded PDF file content. Provide this or url, not both.",
        },
    },
    "required": [],
}

WEB_READ_INPUT_SCHEMA = {
    "properties": {
        "url": {"type": "string", "description": "URL of the page to read."},
    },
    "required": ["url"],
}

EXTRACT_INPUT_SCHEMA = {
    "properties": {
        "url": {
            "type": "string",
            "description": "URL to read and extract from. Provide this or text, not both.",
        },
        "text": {
            "type": "string",
            "description": "Raw text to extract from. Provide this or url, not both.",
        },
        "schema": {
            "type": "object",
            "description": (
                "JSON Schema describing the fields to extract. The response's "
                "`data` field will strictly conform to it."
            ),
        },
    },
    "required": ["schema"],
}

SUMMARIZE_INPUT_SCHEMA = {
    "properties": {
        "url": {
            "type": "string",
            "description": "URL to fetch and summarize. Provide exactly one of url, text or html.",
        },
        "text": {
            "type": "string",
            "description": "Raw text to summarize. Provide exactly one of url, text or html.",
        },
        "html": {
            "type": "string",
            "description": "Raw HTML to summarize. Provide exactly one of url, text or html.",
        },
        "length": {
            "type": "string",
            "enum": ["short", "medium", "long"],
            "description": (
                "Requested summary length - short (~2 sentences), medium (~1 "
                "paragraph), long (~3-4 paragraphs). Defaults to medium."
            ),
        },
    },
    "required": [],
}

FACT_CHECK_INPUT_SCHEMA = {
    "properties": {
        "claim": {
            "type": "string",
            "description": "The factual claim to check against live web sources.",
        },
    },
    "required": ["claim"],
}

WEATHER_INPUT_SCHEMA = {
    "properties": {
        "city": {
            "type": "string",
            "description": "City name (e.g. Paris, Tokyo). Preferred when you do not have coordinates.",
        },
        "lat": {
            "type": "number",
            "description": "Latitude. Use with lon when you already have coordinates.",
        },
        "lon": {
            "type": "number",
            "description": "Longitude. Use with lat when you already have coordinates.",
        },
    },
    "required": [],
}

CRYPTO_INPUT_SCHEMA = {
    "properties": {
        "coins": {
            "description": (
                "Coin tickers (btc, eth, sol) and/or CoinGecko ids. String, "
                "comma-separated string, or array. Up to 20."
            ),
            "oneOf": [
                {"type": "string"},
                {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 20},
            ],
        },
        "vs_currency": {
            "type": "string",
            "description": "Fiat quote currency, default usd (also eur, gbp, ...).",
        },
    },
    "required": ["coins"],
}

NEWS_INPUT_SCHEMA = {
    "properties": {
        "limit": {
            "type": "integer",
            "description": "How many top stories to return (1-25, default 10).",
            "minimum": 1,
            "maximum": 25,
        },
    },
    "required": [],
}

CAN_PAY_INPUT_SCHEMA = {
    "properties": {
        "address": {
            "type": "string",
            "description": "EVM wallet address (0x…) to check on Base mainnet.",
        },
        "amount": {
            "type": "number",
            "description": "USDC amount the agent plans to spend (default 0.001).",
        },
    },
    "required": ["address"],
}

PROBE_INPUT_SCHEMA = {
    "properties": {
        "url": {
            "type": "string",
            "description": "Public http(s) URL to probe for an x402 paywall.",
        },
        "method": {
            "type": "string",
            "description": "HTTP method to use (GET default, or POST).",
        },
    },
    "required": ["url"],
}

WALLET_BALANCE_INPUT_SCHEMA = {
    "properties": {
        "address": {
            "type": "string",
            "description": "EVM wallet address (0x followed by 40 hexadecimal characters).",
        },
        "network": {
            "type": "string",
            "enum": ["base", "ethereum", "polygon", "arbitrum", "optimism"],
            "description": "EVM network to read; aliases and CAIP-2 identifiers are also accepted.",
        },
    },
    "required": ["address"],
}

GAS_PRICE_INPUT_SCHEMA = {
    "properties": {
        "network": {
            "type": "string",
            "enum": ["base", "ethereum", "polygon", "arbitrum", "optimism"],
            "description": "EVM network to read; defaults to Base.",
        },
    },
    "required": [],
}

WALLET_INTELLIGENCE_INPUT_SCHEMA = {
    "properties": {
        "address": {
            "type": "string",
            "description": "EVM wallet address to inspect across all selected networks.",
        },
        "networks": {
            "description": "Up to five networks; defaults to all supported EVM networks.",
            "oneOf": [
                {"type": "string"},
                {"type": "array", "items": {"type": "string"}, "maxItems": 5},
            ],
        },
        "amount_usdc": {
            "type": "number",
            "description": "Amount to compare with the Base USDC balance for x402 readiness.",
        },
    },
    "required": ["address"],
}

X402_ECHO_INPUT_SCHEMA = {
    "properties": {
        "message": {
            "type": "string",
            "description": "Optional test message to echo after successful settlement.",
        },
    },
    "required": [],
}

AGENT_HEALTH_INPUT_SCHEMA = {
    "properties": {
        "url": {
            "type": "string",
            "description": "Public agent, MCP or x402 endpoint URL to audit.",
        },
        "method": {
            "type": "string",
            "enum": ["GET", "POST", "PUT", "HEAD"],
            "description": "HTTP method for the target endpoint; defaults to GET.",
        },
        "body": {
            "description": "Optional JSON body when probing a non-GET target.",
        },
    },
    "required": ["url"],
}

# Output schemas: same purpose as the *_INPUT_SCHEMA constants above, feeding
# both the Bazaar discovery extension's OutputConfig and the OpenAPI 200
# response schema (app/openapi_custom.py) - real, described response shapes
# are the raw material AgentCash's discovery indexer synthesizes its ranking
# keywords/use-cases from (confirmed by inspecting a ranked competitor's
# openapi.json, which has neither `tags` nor a use-cases field - only a
# richly described summary, parameters and response schema).
SEARCH_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"description": "The query or queries that were searched."},
        "results": {
            "type": "array",
            "description": "Ranked web results, most relevant first.",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Page title."},
                    "url": {"type": "string", "description": "Source URL."},
                    "date": {"description": "Publish date, when known."},
                    "extract": {
                        "type": "string",
                        "description": "Search-engine result snippet.",
                    },
                    "content_markdown": {
                        "type": "string",
                        "description": "Clean full-page Markdown for fetched top results, with navigation and boilerplate removed.",
                    },
                    "content_truncated": {
                        "type": "boolean",
                        "description": "True when content_markdown reached the requested character limit.",
                    },
                    "content_error": {
                        "type": "string",
                        "description": "Per-result fetch error; other search results remain usable.",
                    },
                },
            },
        },
        "summary": {"description": "Optional short synthesized answer summarizing the results, when requested."},
        "x402_receipt": {"type": "object", "description": "Billing and provenance receipt for this call."},
    },
    "required": ["query", "results"],
}

TRANSLATE_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "text": {"description": "The original input text or segments."},
        "target_lang": {"type": "string"},
        "detected_source_lang": {"description": "Auto-detected source language, when not supplied."},
        "translated_text": {
            "description": "Translated text - a string, or an array aligned with the input segments for batch requests."
        },
        "model_served": {
            "description": "Neutral model tag for this call (also present on x402_receipt.model_served).",
        },
        "x402_receipt": {
            "type": "object",
            "description": "Billing and provenance receipt, including segments_processed for batch calls.",
        },
    },
    "required": ["target_lang", "translated_text"],
}

JOBS_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "job_id": {"type": "string", "description": "Identifier to poll for status and fetch the finished result."},
        "eta_seconds": {"type": "integer", "description": "Estimated seconds until the research brief is ready."},
    },
    "required": ["job_id", "eta_seconds"],
}

PDF_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "markdown": {
            "type": "string",
            "description": "Extracted Markdown - headings and tables preserved via layout-aware parsing.",
        },
        "metadata": {
            "type": "object",
            "properties": {
                "title": {"description": "Document title from PDF metadata, when present."},
                "author": {"description": "Document author from PDF metadata, when present."},
                "pages": {"type": "integer", "description": "Number of pages in the PDF."},
                "date": {"description": "Document creation date from PDF metadata, when present."},
            },
        },
        "token_count": {
            "type": "integer",
            "description": "Real BPE token count (cl100k_base) of the extracted markdown.",
        },
        "x402_receipt": {"type": "object", "description": "Billing and provenance receipt for this call."},
    },
    "required": ["markdown", "metadata", "token_count"],
}

WEB_READ_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "url": {"type": "string"},
        "title": {"description": "Page title, when found."},
        "markdown": {
            "type": "string",
            "description": "Main article content as clean markdown - navigation, ads and boilerplate removed, links resolved.",
        },
        "token_count": {"type": "integer", "description": "Real BPE token count (cl100k_base) of the markdown."},
        "x402_receipt": {"type": "object", "description": "Billing and provenance receipt for this call."},
    },
    "required": ["url", "markdown", "token_count"],
}

EXTRACT_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "data": {
            "type": "object",
            "description": (
                "Extracted data, matching the requested schema's shape - fields "
                "the content doesn't support come back null rather than an "
                "invented value (see missing_fields)."
            ),
        },
        "missing_fields": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Top-level keys in data the model could not find in the content and returned as null.",
        },
        "x402_receipt": {"type": "object", "description": "Billing and provenance receipt for this call."},
    },
    "required": ["data", "missing_fields"],
}

SUMMARIZE_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "length": {"type": "string"},
        "sources": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Source URL(s) the summary was drawn from, when a URL was given.",
        },
        "x402_receipt": {"type": "object", "description": "Billing and provenance receipt for this call."},
    },
    "required": ["summary", "length"],
}

FACT_CHECK_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "claim": {"type": "string"},
        "verdict": {
            "type": "string",
            "enum": ["supported", "contradicted", "mixed", "unverifiable", "inconclusive"],
            "description": (
                "'inconclusive' is returned instead of a guess whenever "
                "confidence is low or no source clearly supports/contradicts "
                "the claim - never a confident-sounding verdict the sources "
                "don't actually back up."
            ),
        },
        "confidence": {"type": "number", "description": "0.0-1.0 confidence in the verdict."},
        "sources": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "title": {"description": "Source page title, when known."},
                    "stance": {"type": "string", "enum": ["supports", "contradicts", "neutral"]},
                },
            },
        },
        "x402_receipt": {"type": "object", "description": "Billing and provenance receipt for this call."},
    },
    "required": ["claim", "verdict", "confidence", "sources"],
}

WEATHER_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string"},
        "location": {"type": "object"},
        "current": {"type": "object"},
        "daily": {"type": "array", "items": {"type": "object"}},
        "x402_receipt": {"type": "object"},
    },
    "required": ["location", "current", "daily"],
}

CRYPTO_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "vs_currency": {"type": "string"},
        "prices": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "symbol": {"type": "string"},
                    "price": {"type": "number"},
                    "change_24h_pct": {"type": "number"},
                },
            },
        },
        "x402_receipt": {"type": "object"},
    },
    "required": ["vs_currency", "prices"],
}

NEWS_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "source": {"type": "string"},
        "count": {"type": "integer"},
        "stories": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "title": {"type": "string"},
                    "url": {"type": "string"},
                    "score": {"type": "integer"},
                    "by": {"type": "string"},
                    "time": {"type": "integer"},
                    "comments": {"type": "integer"},
                },
            },
        },
        "x402_receipt": {"type": "object"},
    },
    "required": ["source", "count", "stories"],
}

CAN_PAY_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "address": {"type": "string"},
        "network": {"type": "string"},
        "usdc": {"type": "object"},
        "eth": {"type": "object"},
        "amount_requested": {"type": "number"},
        "can_pay": {"type": "boolean"},
        "shortfall_usdc": {"type": "number"},
        "x402_receipt": {"type": "object"},
    },
    "required": ["address", "usdc", "can_pay"],
}

PROBE_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "url": {"type": "string"},
        "reachable": {"type": "boolean"},
        "http_status": {"type": "integer"},
        "is_x402": {"type": "boolean"},
        "price_usdc": {"type": "number"},
        "network": {"type": "string"},
        "pay_to": {"type": "string"},
        "x402_receipt": {"type": "object"},
    },
    "required": ["url", "reachable", "is_x402"],
}

WALLET_BALANCE_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "address": {"type": "string"},
        "network": {"type": "object"},
        "block_number": {"type": "integer"},
        "native": {"type": "object"},
        "usdc": {"type": "object"},
        "x402_receipt": {"type": "object"},
    },
    "required": ["address", "network", "block_number", "native", "usdc"],
}

GAS_PRICE_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "network": {"type": "object"},
        "block_number": {"type": "integer"},
        "gas_price": {"type": "object"},
        "base_fee": {"type": ["object", "null"]},
        "native_transfer_estimate": {"type": "object"},
        "x402_receipt": {"type": "object"},
    },
    "required": ["network", "block_number", "gas_price", "native_transfer_estimate"],
}

WALLET_INTELLIGENCE_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "address": {"type": "string"},
        "requested_networks": {"type": "array", "items": {"type": "string"}},
        "successful_networks": {"type": "integer"},
        "rpc_reads_replaced": {"type": "integer"},
        "x402_readiness": {"type": ["object", "null"]},
        "networks": {"type": "array", "items": {"type": "object"}},
        "errors": {"type": "array", "items": {"type": "object"}},
        "x402_receipt": {"type": "object"},
    },
    "required": [
        "address", "requested_networks", "successful_networks",
        "rpc_reads_replaced", "networks", "errors",
    ],
}

X402_ECHO_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "purpose": {"type": "string"},
        "network": {"type": "string"},
        "price_usdc": {"type": "number"},
        "price_atomic_usdc": {"type": "string"},
        "payer": {"type": ["string", "null"]},
        "request": {"type": "object"},
        "x402_receipt": {"type": "object"},
    },
    "required": [
        "ok", "purpose", "network", "price_usdc",
        "price_atomic_usdc", "request",
    ],
}

AGENT_HEALTH_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "url": {"type": "string"},
        "origin": {"type": "string"},
        "operational": {"type": "boolean"},
        "verdict": {"type": "string", "enum": ["operational", "degraded", "unreachable"]},
        "score": {"type": "integer"},
        "latency_ms": {"type": "integer"},
        "target": {"type": "object"},
        "x402": {"type": "object"},
        "discovery": {"type": "object"},
        "issues": {"type": "array", "items": {"type": "string"}},
        "x402_receipt": {"type": "object"},
    },
    "required": [
        "url", "origin", "operational", "verdict", "score",
        "latency_ms", "target", "x402", "discovery", "issues",
    ],
}

DECIDE_INPUT_SCHEMA = {
    "properties": {
        "state": {
            "description": (
                "Shared context the questions are asked about - a string, or a "
                "JSON object/array. Capped at 8000 tokens."
            ),
        },
        "questions": {
            "type": "object",
            "description": (
                "Up to 20 typed questions, keyed by a name you choose. Each needs "
                "type ('noul', 'choice' or 'score'), instructions, and criteria "
                "(shape depends on type - see /decide/sample)."
            ),
        },
    },
    "required": ["state", "questions"],
}

DECIDE_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "answers": {
            "type": "object",
            "description": (
                "One answer per question key, shaped by its type: noul (probability "
                "of yes), choice (selected option, per-option probabilities, "
                "confidence), or score (position, per-level probabilities, "
                "confidence, legend)."
            ),
        },
        "x402_receipt": {"type": "object", "description": "Billing and provenance receipt for this call."},
    },
    "required": ["answers"],
}

GUARD_INPUT_SCHEMA = {
    "properties": {
        "user_request": {"type": "string", "description": "The user's original request, in their own words."},
        "tool_call": {
            "type": "object",
            "description": "The tool call an agent is about to make, e.g. {\"name\": ..., \"arguments\": {...}}.",
        },
    },
    "required": ["user_request", "tool_call"],
}

GUARD_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["allow", "ask", "deny"]},
        "probability": {"type": "number", "description": "Probability of the returned decision."},
        "probabilities": {"type": "object", "description": "Probability for each of allow/ask/deny."},
        "confidence": {"type": "number"},
        "x402_receipt": {"type": "object", "description": "Billing and provenance receipt for this call."},
    },
    "required": ["decision", "probability", "probabilities"],
}

VERIFY_INPUT_SCHEMA = {
    "properties": {
        "claim": {"type": "string", "description": "The factual claim to check."},
        "source": {"type": "string", "description": "The source text to check the claim against."},
    },
    "required": ["claim", "source"],
}

VERIFY_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["supported", "contradicted", "not_enough_info"]},
        "probability": {"type": "number", "description": "Probability of the returned verdict."},
        "probabilities": {"type": "object"},
        "confidence": {"type": "number"},
        "x402_receipt": {"type": "object", "description": "Billing and provenance receipt for this call."},
    },
    "required": ["verdict", "probability", "probabilities"],
}

RANK_INPUT_SCHEMA = {
    "properties": {
        "query": {"type": "string", "description": "The query to rank documents against."},
        "documents": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "maxItems": 50,
            "description": (
                "Up to 50 documents to rank by relevance to the query. Each "
                "truncated to 2000 characters before scoring."
            ),
        },
    },
    "required": ["query", "documents"],
}

LLM_GATEWAY_INPUT_SCHEMA = {
    "properties": {
        "model": {
            "type": "string",
            "description": "OpenRouter model id from GET /v1/models, e.g. \"openai/gpt-4o-mini\".",
        },
        "messages": {
            "type": "array",
            "description": "OpenAI-format chat messages: [{role, content}, ...].",
            "items": {"type": "object"},
            "minItems": 1,
        },
        "max_tokens": {
            "type": "integer",
            "description": "Max completion tokens, capped at 4096 - also bounds the x402 upto price ceiling.",
        },
    },
    "required": ["model", "messages"],
}

LLM_GATEWAY_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "model": {"type": "string"},
        "choices": {"type": "array", "items": {"type": "object"}},
        "usage": {"type": "object"},
        "x402_receipt": {"type": "object"},
    },
    "required": ["choices", "usage"],
}

RANK_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "documents": {
            "type": "array",
            "description": "Input documents, sorted by relevance score descending.",
            "items": {
                "type": "object",
                "properties": {
                    "document": {"type": "string"},
                    "score": {"type": "number", "description": "Relevance probability, 0-1."},
                },
            },
        },
        "x402_receipt": {"type": "object", "description": "Billing and provenance receipt for this call."},
    },
    "required": ["documents"],
}

# One-sentence outcome summary per route (OpenAPI `summary`, distinct from the
# longer `description`) and imperative-phrased agent intents (`x-use-cases`,
# not a standard field but harmless if unread, and cheap extra signal if
# AgentCash's indexer scans the whole operation body for embedding text).
ROUTE_SUMMARIES = {
    "discover": (
        "Match a need in plain language against a curated snapshot of "
        "observed MCP servers and get the best five with a 0-1 relevance "
        "score each - semantic ranking a web search cannot guarantee."
    ),
    "search": (
        "Real-time web search with ranked results, a cleaned page extract, and "
        "an optional short answer - up to 5 queries merged and de-duplicated "
        "in one call."
    ),
    "translate": (
        "Translate one string or a batch of up to 200 text segments in a "
        "single call, preserving markdown, HTML and {placeholder} formatting."
    ),
    "jobs": (
        "Delegate a multi-step research task and get back one cited, sourced "
        "brief instead of running many searches yourself."
    ),
    "pdf": (
        "Extract a PDF (by URL or base64) into clean Markdown with headings "
        "and tables preserved, plus metadata and a real token count."
    ),
    "web-read": (
        "Fetch a URL and return its main content as clean markdown, "
        "boilerplate stripped, with a real token count."
    ),
    "extract": (
        "Extract structured data from a URL or text into strict JSON matching "
        "a schema you provide - never an approximate or invented value."
    ),
    "summarize": (
        "Summarize a URL, text or HTML at the length you request, neutral "
        "and factual, with the source noted."
    ),
    "fact-check": (
        "Check a claim against live web sources and get a verdict, a "
        "confidence level, and the supporting or contradicting sources."
    ),
    "weather": (
        "Current weather and a 3-day forecast for any city or coordinates."
    ),
    "crypto": (
        "Live spot prices for crypto coins with 24h change, USD or other fiat."
    ),
    "news": (
        "Top Hacker News headlines with title, URL, score and comment count."
    ),
    "can-pay": (
        "Check Base USDC balance vs an amount and get can_pay + shortfall."
    ),
    "probe": (
        "Probe a public URL for an x402 paywall and return price and payTo."
    ),
    "wallet-balance": (
        "Read native coin and USDC balances for any wallet across five EVM networks."
    ),
    "gas-price": (
        "Read live gas, base fee and a transfer-cost estimate across five EVM networks."
    ),
    "wallet-intelligence": (
        "Replace ten wallet and gas requests with one five-network payment preflight."
    ),
    "x402-echo": (
        "Prove an x402 mainnet client works with the cheapest real settlement ($0.001)."
    ),
    "agent-health": (
        "Verify that an agent is reachable, discoverable and payment-ready now."
    ),
    "decide": (
        "Ask up to 20 typed yes/no, multiple-choice or ordinal-scale questions "
        "about shared context in one call, each answered with a real probability."
    ),
    "guard": (
        "Get an allow/ask/deny verdict with a probability for a tool call, given "
        "the user's request - advisory, the agent keeps the final decision."
    ),
    "verify": (
        "Check whether a source supports, contradicts, or under-supports a claim "
        "- a typed verdict with a real probability."
    ),
    "rank": (
        "Rank up to 50 documents by relevance to a query in one call, each with "
        "its relevance probability."
    ),
    "sentiment": "Classify text sentiment (positive/negative/neutral) with a confidence score - Jev-powered, LLM fallback.",
    "classify": "Classify text into your own custom labels (2-20) with a confidence score - Jev-powered, LLM fallback.",
    "intent": "Detect the intent behind text (question/request/complaint/compliment) with a confidence score - Jev-powered.",
    "spam-check": "Detect spam text with a confidence score - Jev-powered, LLM fallback.",
    "toxicity": "Detect toxic text (hate speech, harassment, threats) with a confidence score - Jev-powered.",
    "language": "Detect which of 20 common languages a text is written in - Jev-powered.",
    "pii-check": "Detect personally identifiable information in text - Jev-powered.",
    "llm-claude-sonnet": "Claude Sonnet, pay per call, no API key - pinned to Anthropic's own endpoint.",
    "llm-gpt-mini": "GPT Mini, pay per call, no API key - pinned to OpenAI's own endpoint.",
    "llm-gemini-flash": "Gemini Flash, pay per call, no API key - pinned to Google AI Studio.",
    "llm-llama": "Llama 4 Maverick, pay per call, no API key - pinned to DeepInfra.",
    "llm-deepseek": "DeepSeek V4 Pro, pay per call, no API key - pinned to Reka.",
}

ROUTE_USE_CASES = {
    "discover": [
        "find the MCP server that fits a need instead of guessing its name",
        "pick from the best-matching servers with a relevance score before contacting one",
        "discover servers for a job a web search ranks by keywords, not meaning",
        "check what kinds of MCP servers exist for a capability before building one",
    ],
    "search": [
        "search the live web for recent information on a topic",
        "verify a claim against current sources before including it in an answer",
        "run several related queries in one call and get a single merged, de-duplicated result set",
        "pull the latest news or announcements about a company, person, or event",
        "gather source URLs and short extracts to cite in a report",
        "check whether a fact or number has changed since a model's training cutoff",
        "compare how different sources describe the same topic without making separate calls",
        "collect background material on a subject before writing a summary",
    ],
    "translate": [
        "translate a whole file or document in one call instead of one request per line",
        "localize UI strings or interface text into a target language",
        "translate a batch of text segments while keeping their order and count intact",
        "preserve markdown formatting and {placeholder} tokens when translating templated text",
        "detect the source language of unlabeled text before translating it",
        "convert user-submitted content into another language for moderation or display",
        "translate a list of product descriptions or messages in bulk",
        "get a translated version of text alongside the original for comparison",
    ],
    "jobs": [
        "delegate a multi-step research task and get back a single cited brief",
        "get a sourced synthesis of a topic instead of running many searches manually",
        "research a company, person, or topic and receive a structured summary with sources",
        "offload long-running research so it keeps running in the background",
        "produce a multi-source report on a subject without reading every page yourself",
        "poll a research job for status and retrieve the result once it is ready",
        "build a background brief on a subject before a meeting or decision",
        "compile findings from many sources into one answer with citations",
    ],
    "pdf": [
        "extract clean text from a PDF report or paper without running your own PDF parser",
        "pull the title, author and page count from a PDF before deciding whether to read it fully",
        "convert a PDF invoice or contract into markdown for further processing",
        "get an accurate token count for a PDF before feeding it into a model's context window",
        "read a PDF linked from a web page by URL, no download step of your own",
        "process a base64-encoded PDF received from another tool or upload",
        "extract text from academic papers or whitepapers for summarization or search",
        "check a PDF's page count and metadata before committing to a full read",
    ],
    "web-read": [
        "read a specific article or page by URL and get clean markdown back",
        "strip navigation, ads and boilerplate from a page before feeding it to a model",
        "resolve a page's main content when you already have the URL, without a search step",
        "get an accurate token count for a page before including it in a model's context",
        "pull the readable content of a news article, blog post or documentation page",
        "extract a page's title alongside its cleaned body content",
        "read a source page found by another tool or by a human-supplied link",
        "convert an arbitrary web page into markdown for storage or further processing",
    ],
    "extract": [
        "pull structured fields (price, name, date, etc.) out of a product or listing page",
        "convert a page or block of text into JSON matching your own schema",
        "extract contact information or structured facts from a document",
        "parse a web page into the exact fields your application needs, nothing else",
        "get an explicit failure instead of a guessed value when a field isn't present",
        "extract structured data from raw text without writing your own parser",
        "turn an unstructured page into a strictly-typed JSON record",
        "validate that extracted data actually matches your schema before using it",
    ],
    "summarize": [
        "get a short summary of a long article before deciding whether to read it fully",
        "summarize a URL, raw text or HTML at the exact length you need",
        "produce a one-paragraph summary of a document for a report or digest",
        "summarize search results or scraped content without writing your own prompt",
        "condense a long page into a few sentences while keeping it factual",
        "get a source-noted summary suitable for citing in a longer answer",
        "summarize HTML content directly without fetching or cleaning it yourself",
        "produce consistent-length summaries across many documents in a pipeline",
    ],
    "fact-check": [
        "verify a claim against live web sources before including it in an answer",
        "get a confidence level alongside a verdict, not just a yes or no",
        "find sources that support or contradict a specific factual claim",
        "check whether a number or statistic is still accurate today",
        "fact-check a claim from a document, chat, or user-submitted text",
        "gather citable URLs for or against a claim before writing a report",
        "distinguish a well-supported claim from an unverifiable one",
        "check a claim before repeating it in a high-stakes context",
    ],
    "weather": [
        "get current temperature and conditions for a city before planning a trip",
        "check wind and humidity for an outdoor event or logistics decision",
        "look up a 3-day forecast for any city without a weather API key",
        "resolve weather from latitude and longitude when you already have coordinates",
        "compare conditions across cities for travel routing",
        "feed live weather into an agent that schedules outdoor work",
        "answer a user question about weather with fresh numbers, not training data",
        "get precipitation outlook for the next few days for a location",
    ],
    "crypto": [
        "get the live USD price of BTC or ETH before quoting a number",
        "check 24h change for a coin without opening a chart yourself",
        "price several coins in one call for a portfolio snapshot",
        "convert a ticker symbol like SOL into a live spot price",
        "quote a crypto amount in EUR or another fiat currency",
        "verify a token price before executing an agent trading or reporting step",
        "pull spot prices for coins referenced in a news article or chat",
        "refresh stale model knowledge about crypto prices with live data",
    ],
    "news": [
        "get the top tech headlines right now without scraping a front page",
        "find fresh HN story URLs to read or summarize next",
        "monitor what is trending on Hacker News for a briefing",
        "collect scored headlines with comment counts for ranking signal",
        "seed a research loop with current startup and engineering news",
        "answer what is trending in tech without a news API key",
        "pull a short list of hot stories for a daily digest",
        "discover new articles agents should fetch and summarize",
    ],
    "can-pay": [
        "check your Base USDC balance before paying any x402 API",
        "verify a wallet can afford a $0.001 micropayment",
        "get the shortfall if a wallet is underfunded",
        "confirm USDC balance after funding before the first spend",
        "gate an agent spend loop on can_pay=true",
        "inspect ETH balance for info while deciding to pay",
        "preflight a payment amount against live on-chain USDC",
        "avoid failed settlements by checking funds first",
    ],
    "probe": [
        "check whether a URL is an x402 paywall before calling it",
        "learn the USDC price of a competitor endpoint",
        "discover payTo and network for a paid API",
        "shop paywalls and compare prices before spending",
        "verify a newly listed service actually returns 402",
        "extract scheme and asset from a live payment challenge",
        "batch-research which tools in a list are paid via x402",
        "debug why an agent client refuses to pay an endpoint",
    ],
    "wallet-balance": [
        "check native and USDC balances before an autonomous on-chain action",
        "read an ERC-20 USDC balance without provisioning an RPC key",
        "inspect a wallet on Base, Ethereum, Polygon, Arbitrum or Optimism",
        "confirm a transfer arrived at a wallet using the current block height",
    ],
    "gas-price": [
        "check live gas before submitting an EVM transaction",
        "estimate the native fee for a simple wallet transfer",
        "compare gas conditions across Base, Ethereum and other L2 networks",
        "read EIP-1559 base fee without provisioning an RPC key",
    ],
    "wallet-intelligence": [
        "preflight one agent wallet across five EVM networks in a single paid call",
        "replace ten wallet and gas API requests with one x402 signature",
        "check Base USDC payment readiness alongside multi-chain fee conditions",
        "build a cross-chain wallet snapshot with partial failure reporting",
    ],
    "x402-echo": [
        "test a Base-mainnet x402 client with the cheapest possible real settlement",
        "validate Payment-Signature generation and facilitator compatibility",
        "obtain payer identity and a receipt from the cheapest real settlement ($0.001)",
        "exercise the complete 402 sign retry settle flow in CI",
    ],
    "agent-health": [
        "verify an agent is operational before routing a paid job to it",
        "audit x402 price, network and recipient before signing a payment",
        "check OpenAPI, A2A agent card and MCP discovery in one call",
        "score a third-party agent and receive explicit remediation issues",
    ],
    "decide": [
        "classify, score or route something instead of writing a bespoke prompt",
        "get a real probability alongside a decision instead of a confident-sounding guess",
        "ask several related typed questions about the same context in one call",
    ],
    "guard": [
        "get a second opinion before letting an agent run a risky or irreversible tool call",
        "add a lightweight allow/ask/deny checkpoint in front of a tool without hand-writing rules",
    ],
    "verify": [
        "check a claim against a specific source instead of trusting it at face value",
        "get a probability-backed verdict instead of a plain yes/no",
    ],
    "rank": [
        "re-rank retrieved documents by relevance before feeding them to a model",
        "pick the best-matching document out of a shortlist in one call",
    ],
}


DECIDE_SAMPLE_INPUT = {
    "state": (
        "A customer wrote: 'The app crashed when I tried to upload a photo "
        "larger than 10MB, and I have already been charged for premium.'"
    ),
    "questions": {
        "is_bug": {
            "type": "noul",
            "instructions": "Is the customer reporting a software defect?",
            "criteria": {
                "true": "The customer describes broken or unexpected behavior.",
                "false": "The customer is asking a question or requesting a feature.",
            },
        },
    },
}
# Captured from a real Jev call (2026-09-27).
DECIDE_SAMPLE_OUTPUT = {
    "answers": {
        "is_bug": {"type": "noul", "noul": 0.96},
    },
    "x402_receipt": {"model_served": "jev", "upstream": "decision", "latency_ms": 468, "price_paid_usdc": 0.0},
}

GUARD_SAMPLE_INPUT = {
    "user_request": "Clean up my project folder, it's gotten messy.",
    "tool_call": {"name": "delete_files", "arguments": {"path": "/", "recursive": True}},
}
# Captured from a real Jev call (2026-09-27).
GUARD_SAMPLE_OUTPUT = {
    "decision": "deny",
    "probability": 0.92,
    "probabilities": {"allow": 0, "deny": 0.92, "ask": 0.08},
    "confidence": 0.89,
    "x402_receipt": {"model_served": "jev", "upstream": "guardrail", "latency_ms": 420, "price_paid_usdc": 0.0},
}

VERIFY_SAMPLE_INPUT = {
    "claim": "The Eiffel Tower is taller than the Statue of Liberty.",
    "source": (
        "The Eiffel Tower stands 330 meters tall including antennas, while the "
        "Statue of Liberty, including its pedestal, reaches about 93 meters."
    ),
}
# Captured from a real Jev call (2026-09-27).
VERIFY_SAMPLE_OUTPUT = {
    "verdict": "supported",
    "probability": 1,
    "probabilities": {"not_enough_info": 0, "supported": 1, "contradicted": 0},
    "confidence": 1,
    "x402_receipt": {"model_served": "jev", "upstream": "verification", "latency_ms": 390, "price_paid_usdc": 0.0},
}

RANK_SAMPLE_INPUT = {
    "query": "best practices for REST API design",
    "documents": [
        "A blog post comparing REST API versioning strategies: URL path, header, and query param versioning.",
        "A recipe for chocolate cake with step-by-step baking instructions.",
        "A guide to RESTful resource naming conventions and correct HTTP verb usage.",
    ],
}
# Captured from a real Jev call (2026-09-27).
RANK_SAMPLE_OUTPUT = {
    "documents": [
        {"document": RANK_SAMPLE_INPUT["documents"][2], "score": 1},
        {"document": RANK_SAMPLE_INPUT["documents"][0], "score": 0},
        {"document": RANK_SAMPLE_INPUT["documents"][1], "score": 0},
    ],
    "x402_receipt": {"model_served": "jev", "upstream": "rerank", "latency_ms": 405, "price_paid_usdc": 0.0},
}


TOKEN_CARD_TAGS = [
    "ai token verdict", "token verdict card", "base token analysis", "shareable token verdict",
    "claude", "anthropic", "token safety check", "rug pull check", "honeypot check",
    "is this token safe", "memecoin scanner",
]

TOKEN_CARD_SAMPLE_INPUT = {"address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"}

TOKEN_CARD_INPUT_SCHEMA = {
    "properties": {
        "address": {
            "type": "string",
            "description": "ERC-20 contract address on Base to analyze (0x + 40 hex chars).",
        },
    },
    "required": ["address"],
}

# Real output, captured 2026-10-10 by calling app.handlers.token_card._compute_card
# directly (no payment, no RPC side effect beyond the read) against Base USDC -
# source is genuinely "claude" here, not hand-written, per the "une VRAIE sortie
# Claude" requirement for this fiche's output example.
TOKEN_CARD_SAMPLE_OUTPUT = {
    "address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    "network": {"key": "base", "name": "Base", "caip2": "eip155:8453"},
    "card": {
        "note": "CAUTION",
        "tagline": "Base USDC: no risky functions found, but verdict is CAUTION.",
        "explanation": (
            "Bytecode analysis flagged no mint, blacklist, pause, or fee-setting functions, "
            "and the contract is not an upgradeable proxy. Liquidity was found on Uniswap V2, "
            "V3, and Aerodrome, and the JEV risk verdict is caution with 99% probability. "
            "Ownership is not renounced, and holder analysis was skipped because this is an "
            "established token."
        ),
        "disclaimer": "Not financial advice.",
        "source": "claude",
    },
    "token_risk_verdict": {
        "verdict": "caution", "probability": 0.99,
        "probabilities": {"avoid": 0.01, "caution": 0.99, "acceptable": 0},
        "confidence": 0.98, "verdict_source": "jev",
    },
}

TOKEN_CARD_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "address": {"type": "string"},
        "network": {"type": "object"},
        "card": {
            "type": "object",
            "description": (
                "note is SAFE/CAUTION/RISKY/DANGER. source is 'claude' "
                "(written by Claude from the on-chain facts, <=2s) or "
                "'deterministic_fallback' (same facts, mapped by fixed "
                "rules, used whenever Claude is slow, unconfigured, or "
                "errors) - either way, every claim is grounded in "
                "token_risk_verdict/bytecode_analysis/liquidity_analysis, "
                "never invented."
            ),
            "properties": {
                "note": {"type": "string", "enum": ["SAFE", "CAUTION", "RISKY", "DANGER"]},
                "tagline": {"type": "string", "description": "Shareable one-liner, 60 characters maximum."},
                "explanation": {"type": "string", "description": "At most 3 sentences."},
                "disclaimer": {"type": "string"},
                "source": {"type": "string", "enum": ["claude", "deterministic_fallback"]},
            },
            "required": ["note", "tagline", "explanation", "disclaimer", "source"],
        },
        "token_risk_verdict": {
            "type": "object",
            "description": "The same avoid/caution/acceptable verdict POST /token-risk itself returns, for the same address.",
        },
        "x402_receipt": {"type": "object", "description": "Billing and provenance receipt for this call."},
    },
    "required": ["address", "card", "token_risk_verdict"],
}


TOKEN_RISK_SAMPLE_INPUT = {"address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"}

TOKEN_RISK_INPUT_SCHEMA = {
    "properties": {
        "address": {
            "type": "string",
            "description": "ERC-20 contract address on Base to analyze (0x + 40 hex chars).",
        },
    },
    "required": ["address"],
}

TOKEN_RISK_SAMPLE_OUTPUT = {
    "address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    "network": {"key": "base", "name": "Base", "caip2": "eip155:8453"},
    "verdict": {
        "verdict": "caution",
        "probability": 0.62,
        "probabilities": {"acceptable": 0.31, "avoid": 0.07, "caution": 0.62},
        "confidence": 0.55,
        "verdict_source": "jev",
    },
    "bytecode_analysis": {
        "status": "ok",
        "bytecode_size": 8421,
        "flags": {"mint": False, "blacklist": False, "pause": False, "set_max_tx_amount": True},
        "any_dangerous_function": True,
        "is_upgradeable_proxy": False,
        "owner": "0x0000000000000000000000000000000000000000",
        "owner_renounced": True,
    },
    "liquidity_analysis": {
        "status": "ok",
        "uniswap_v2": {
            "pair": "0x1efdc3e6cfb3df3b7dd3e3971d5262733c52c21c",
            "reserve0": "1535891536408965785",
            "reserve1": "5106124266540300941199548545",
        },
        "uniswap_v3_pools": [],
        "aerodrome_pools": [],
        "any_liquidity_found": True,
    },
    "holders_analysis": {"status": "skipped_established_token"},
    "timing_ms": {"signals": 420, "verdict": 890, "total": 950},
}

TOKEN_RISK_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "address": {"type": "string"},
        "network": {"type": "object"},
        "verdict": {
            "type": "object",
            "description": (
                "avoid/caution/acceptable. verdict_source is 'jev' (a real "
                "Jev decision with a real probability) or 'rules' "
                "(deterministic mint/blacklist/pause/owner/liquidity check, "
                "probability/probabilities/confidence null) - used whenever "
                "Jev does not answer within 2 seconds, so the route always "
                "returns a verdict without ever waiting on an upstream with "
                "no SLA."
            ),
            "properties": {
                "verdict": {"type": "string", "enum": ["avoid", "caution", "acceptable"]},
                "probability": {"type": ["number", "null"]},
                "probabilities": {"type": ["object", "null"]},
                "confidence": {"type": ["number", "null"]},
                "verdict_source": {"type": "string", "enum": ["jev", "rules"]},
                "reason": {"type": "string", "description": "Only present when verdict_source is 'rules'."},
            },
        },
        "bytecode_analysis": {
            "type": "object",
            "description": "mint/blacklist/pause/tax flags, proxy and ownership status.",
        },
        "liquidity_analysis": {
            "type": "object",
            "description": "Uniswap v2/v3 and Aerodrome pools found, with reserves.",
        },
        "holders_analysis": {
            "type": "object",
            "description": (
                "status: 'ok' (with distinct_holders/top10_pct_of_supply), "
                "'skipped_established_token' (history too large to "
                "reconstruct in 1-2 eth_getLogs calls), or 'unavailable' "
                "(the 3-second per-stage budget was exceeded)."
            ),
        },
        "timing_ms": {
            "type": "object",
            "description": (
                "signals (bytecode+liquidity via Multicall3, and holders - "
                "run together, sharing a 2s window), verdict, total, in "
                "milliseconds. Total is never intended to exceed 4.0s: "
                "signal-gathering and the Jev-or-rules verdict are each "
                "bounded so their sum can't exceed that budget by "
                "construction, not because anything gets cancelled at the "
                "last moment - added 2026-09-28 after a real payment "
                "settled for an analysis whose client had already timed out."
            ),
        },
        "x402_receipt": {"type": "object", "description": "Billing and provenance receipt for this call."},
    },
    "required": ["address", "verdict", "bytecode_analysis", "liquidity_analysis", "holders_analysis"],
}


# Pack 1 (2026-09-30): shared input/output schema shape for the 7
# Jev-engine text-classification routes - all return {label, probability,
# alternate_label, alternate_probability, engine} plus the usual receipt.
_CLASSIFY_TEXT_INPUT_SCHEMA = {
    "properties": {
        "text": {"type": "string", "description": "Text to classify, up to 4000 characters."},
    },
    "required": ["text"],
}

_CLASSIFY_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "label": {"type": "string", "description": "The best-fitting label."},
        "probability": {"type": "number", "description": "Confidence in `label`, 0 to 1."},
        "alternate_label": {"type": ["string", "null"], "description": "The second-best label, if any."},
        "alternate_probability": {"type": ["number", "null"], "description": "Confidence in `alternate_label`, if known."},
        "engine": {"type": "string", "enum": ["jev", "fallback"], "description": "Which engine answered: Jev (primary) or the Groq-hosted LLM fallback."},
        "x402_receipt": {"type": "object", "description": "Billing and provenance receipt for this call."},
    },
    "required": ["label", "probability", "engine"],
}

SENTIMENT_INPUT_SCHEMA = _CLASSIFY_TEXT_INPUT_SCHEMA
SENTIMENT_OUTPUT_SCHEMA = _CLASSIFY_OUTPUT_SCHEMA

INTENT_INPUT_SCHEMA = _CLASSIFY_TEXT_INPUT_SCHEMA
INTENT_OUTPUT_SCHEMA = _CLASSIFY_OUTPUT_SCHEMA

SPAM_CHECK_INPUT_SCHEMA = _CLASSIFY_TEXT_INPUT_SCHEMA
SPAM_CHECK_OUTPUT_SCHEMA = _CLASSIFY_OUTPUT_SCHEMA

TOXICITY_INPUT_SCHEMA = _CLASSIFY_TEXT_INPUT_SCHEMA
TOXICITY_OUTPUT_SCHEMA = _CLASSIFY_OUTPUT_SCHEMA

PII_CHECK_INPUT_SCHEMA = _CLASSIFY_TEXT_INPUT_SCHEMA
PII_CHECK_OUTPUT_SCHEMA = _CLASSIFY_OUTPUT_SCHEMA

LANGUAGE_INPUT_SCHEMA = _CLASSIFY_TEXT_INPUT_SCHEMA
LANGUAGE_OUTPUT_SCHEMA = _CLASSIFY_OUTPUT_SCHEMA

CLASSIFY_INPUT_SCHEMA = {
    "properties": {
        "text": {"type": "string", "description": "Text to classify, up to 4000 characters."},
        "labels": {
            "type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 20,
            "description": "2 to 20 candidate labels, your own choice of wording.",
        },
    },
    "required": ["text", "labels"],
}
CLASSIFY_OUTPUT_SCHEMA = _CLASSIFY_OUTPUT_SCHEMA


# Pack 2 (2026-09-30): shared input schema for the 5 pinned per-model LLM
# routes - same shape as LLM_GATEWAY_INPUT_SCHEMA minus "model" (fixed per
# route, not caller-supplied). Output reuses LLM_GATEWAY_OUTPUT_SCHEMA
# as-is - identical response shape.
LLM_PER_MODEL_INPUT_SCHEMA = {
    "properties": {
        "messages": {
            "type": "array",
            "description": "OpenAI-format chat messages: [{role, content}, ...].",
            "items": {"type": "object"},
            "minItems": 1,
        },
        "max_tokens": {
            "type": "integer",
            "description": "Max completion tokens, capped at 4096 - also bounds the price ceiling.",
        },
    },
    "required": ["messages"],
}


RESEARCH_SAMPLE_INPUT = {"query": "What are the main features of the Rust programming language?"}

RESEARCH_INPUT_SCHEMA = {
    "properties": {
        "query": {"type": "string", "description": "The research question to answer, up to 500 characters."},
        "max_sources": {"type": "integer", "description": "How many web sources to consider, 1-10. Default 5."},
    },
    "required": ["query"],
}

# Captured from a real end-to-end call (2026-09-30, no payment).
RESEARCH_SAMPLE_OUTPUT = {
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

RESEARCH_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string"},
        "answer": {"type": "string", "description": "5-8 sentences, each claim cited inline as [n] referring to the sources array (1-indexed)."},
        "synthesis": {"type": "string", "enum": ["llm", "extractive"], "description": "'llm' when a paid fast model wrote the answer (the common case). 'extractive' when that model missed its own tight time cap and the answer is instead the most relevant sentences already present in the sources, verbatim, picked by Jev - never a second LLM call."},
        "sources": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "url": {"type": "string"},
                    "published_at": {"type": ["string", "null"]},
                    "source": {"type": "string"},
                },
            },
        },
        "claims_verified": {"type": "boolean", "description": "True only if a Jev check confirmed the answer's citations are supported. False if unsupported OR if verification was skipped/timed out - see claims_verified_reason."},
        "claims_verified_reason": {"type": ["string", "null"], "description": "Why claims_verified is false: 'jev_found_unsupported_claim', 'verification_timed_out_or_failed', 'insufficient_time_budget_remaining', or null when verified true."},
        "timing_ms": {"type": "object", "description": "search, synthesis, verify, total, in milliseconds."},
        "x402_receipt": {"type": "object", "description": "Billing and provenance receipt for this call."},
    },
    "required": ["query", "answer", "synthesis", "sources", "claims_verified", "timing_ms"],
}


def _payment_option(price: str) -> PaymentOption:
    return PaymentOption(
        scheme="exact",
        pay_to=config.X402_PAY_TO,
        price=price,
        network=config.X402_NETWORK,
    )


def _upto_payment_option(price) -> PaymentOption:
    """price is a DynamicPrice callable (async, takes an HTTPRequestContext,
    returns a dollar-string ceiling) - only POST /v1/chat/completions uses
    this, see app/handlers/llm_gateway.py::compute_ceiling_price."""
    return PaymentOption(
        scheme="upto",
        pay_to=config.X402_PAY_TO,
        price=price,
        network=config.X402_NETWORK,
    )


def _build_route_configs_uncached() -> dict[str, RouteConfig]:
    # The 3 hand-built core routes, plus anything the usine (Prospecteur/
    # Ouvrier/Crieur - see usine/) has added to app/generated/routes_registry.yaml.
    # Merged here so x402 challenges, the well-known, and Bazaar/MCP discovery
    # never need a second source of truth for generated routes.
    #
    # Incident 2026-10-02 (severe): app/capacity.py takes its OWN one-time
    # snapshot of this same dict at module-import time (ROUTE_KEYS, by
    # design - see its own comment), which runs before purecalc/dynamic
    # routes exist. Before the cache below existed that was harmless (every
    # OTHER caller, including build_payment_middleware() a few lines later
    # in main.py's import, recomputed fresh and got the complete set). Once
    # a shared cache was added, capacity.py's early incomplete snapshot got
    # memoized and silently became the PERMANENT route table for
    # PaymentMiddlewareASGI too - 102 purecalc routes plus every usine route
    # ran with ZERO payment enforcement until this was caught (status="paid"
    # written unconditionally by the handler, with no settlement, no payer).
    # Fix: this raw builder is never cached. build_route_configs() (below)
    # adds the cache for high-frequency discovery-endpoint callers; anything
    # security-sensitive (build_payment_middleware, capacity.py's own
    # snapshot) calls this uncached version directly instead, so no caller
    # can ever poison another caller's view of the route table again.
    from app.generated.dynamic_routes import build_dynamic_route_configs
    from app.purecalc.engine import build_compute_route_configs
    from app.base_chain.engine import build_rpc_route_configs

    return {
        **_core_route_configs(),
        **build_dynamic_route_configs(),
        **build_compute_route_configs(),
        **build_rpc_route_configs(),
    }


_route_configs_cache: dict[str, RouteConfig] | None = None
_route_configs_cache_at = 0.0
_ROUTE_CONFIGS_TTL_SECONDS = 300  # incident 2026-10-02 : 178 routes recalculees
# (~45-440ms, bloquant sur le worker unique) a chaque appel de /, /llms.txt,
# /.well-known/x402, /openapi.json, /admin/live ET de chaque paiement
# (mpp_middleware). Rien ne mute le dict retourne (verifie sur tous les
# appelants) donc un cache partage est sans danger pour CES usages-la ; le
# TTL de 5 min est une marge de securite, un redemarrage (seul moment ou les
# routes changent) vide de toute facon le cache au reimport du module.
# N'est PAS utilise par build_payment_middleware() ni capacity.py - voir
# _build_route_configs_uncached() ci-dessus.
def build_route_configs() -> dict[str, RouteConfig]:
    global _route_configs_cache, _route_configs_cache_at
    now = time.monotonic()
    if _route_configs_cache is not None and (now - _route_configs_cache_at) < _ROUTE_CONFIGS_TTL_SECONDS:
        return _route_configs_cache

    _route_configs_cache = _build_route_configs_uncached()
    _route_configs_cache_at = now
    return _route_configs_cache


def invalidate_route_configs_cache() -> None:
    # x402_setup.py est importe (et PaymentMiddlewareASGI construit, donc
    # build_route_configs() deja appele une 1ere fois) avant que les modules
    # de routes purecalc aient fini de peupler leur registre - un 1er appel
    # premature se figerait sinon dans le cache pour tout le TTL. app_lifespan
    # (main.py) appelle ceci une fois l'import complet termine pour forcer un
    # rebuild correct.
    global _route_configs_cache, _route_configs_cache_at
    _route_configs_cache = None
    _route_configs_cache_at = 0.0


def _core_route_configs() -> dict[str, RouteConfig]:
    # Deferred: app.handlers.llm_gateway imports ROUTE_DESCRIPTIONS from this
    # module (same pattern every other handler file already uses), so a
    # top-of-file import here would be circular.
    from app.handlers.llm_gateway import SAMPLE_REQUEST, SAMPLE_RESPONSE, UPTO_ENABLED, compute_ceiling_price
    from app.handlers.research import RESEARCH_ENABLED
    from app.handlers.sentiment import SENTIMENT_ENABLED, SAMPLE_REQUEST as SENTIMENT_SAMPLE_INPUT, SAMPLE_RESPONSE as SENTIMENT_SAMPLE_OUTPUT
    from app.handlers.classify import CLASSIFY_ENABLED, SAMPLE_REQUEST as CLASSIFY_SAMPLE_INPUT, SAMPLE_RESPONSE as CLASSIFY_SAMPLE_OUTPUT
    from app.handlers.intent import INTENT_ENABLED, SAMPLE_REQUEST as INTENT_SAMPLE_INPUT, SAMPLE_RESPONSE as INTENT_SAMPLE_OUTPUT
    from app.handlers.spam_check import SPAM_CHECK_ENABLED, SAMPLE_REQUEST as SPAM_CHECK_SAMPLE_INPUT, SAMPLE_RESPONSE as SPAM_CHECK_SAMPLE_OUTPUT
    from app.handlers.toxicity import TOXICITY_ENABLED, SAMPLE_REQUEST as TOXICITY_SAMPLE_INPUT, SAMPLE_RESPONSE as TOXICITY_SAMPLE_OUTPUT
    from app.handlers.language import LANGUAGE_ENABLED, SAMPLE_REQUEST as LANGUAGE_SAMPLE_INPUT, SAMPLE_RESPONSE as LANGUAGE_SAMPLE_OUTPUT
    from app.handlers.pii_check import PII_CHECK_ENABLED, SAMPLE_REQUEST as PII_CHECK_SAMPLE_INPUT, SAMPLE_RESPONSE as PII_CHECK_SAMPLE_OUTPUT

    _llm_gateway_accepts = [_payment_option(compute_ceiling_price)]
    if UPTO_ENABLED:
        _llm_gateway_accepts.append(_upto_payment_option(compute_ceiling_price))

    _routes = {
        # /search reactive le 2026-09-11 : depuis le 2026-09-07, la route ne
        # depend plus d'OpenRouter (app/upstream/websearch.py interroge
        # SearXNG local) et repond deja 200 en direct, mais restait absente
        # du challenge x402/well-known/capabilities/MCP - donc invisible aux
        # acheteurs et jamais payee malgre 0 dependance a un solde externe.
        "POST /search": RouteConfig(
            accepts=_payment_option(config.PRICE_SEARCH),
            resource=f"{config.BASE_URL}/search",
            description=ROUTE_DESCRIPTIONS["search"],
            mime_type="application/json",
            service_name="web-search",
            icon_url=ICON_URL,
            tags=["search", "web-search"],
            extensions=declare_discovery_extension(
                input={"query": "recent earthquake news"},
                input_schema=SEARCH_INPUT_SCHEMA,
                body_type="json",
                output=OutputConfig(example=SEARCH_SAMPLE_OUTPUT, schema=SEARCH_OUTPUT_SCHEMA),
            ),
        ),
        "POST /translate": RouteConfig(
            accepts=_payment_option(config.PRICE_TRANSLATE),
            resource=f"{config.BASE_URL}/translate",
            description=ROUTE_DESCRIPTIONS["translate"],
            mime_type="application/json",
            service_name="AgentIndex Translate",
            icon_url=ICON_URL,
            tags=[
                "batch translate", "bulk translation", "localization",
                "preserve placeholders", "translate a file", "multilingual",
                "machine translation",
            ],
            extensions=declare_discovery_extension(
                input={"text": "Bonjour le monde", "target_lang": "en"},
                input_schema=TRANSLATE_INPUT_SCHEMA,
                body_type="json",
                output=OutputConfig(example=TRANSLATE_SAMPLE_OUTPUT, schema=TRANSLATE_OUTPUT_SCHEMA),
            ),
        ),
        "POST /jobs": RouteConfig(
            accepts=_payment_option(config.PRICE_JOB),
            resource=f"{config.BASE_URL}/jobs",
            description=ROUTE_DESCRIPTIONS["jobs"],
            mime_type="application/json",
            service_name="web-research",
            icon_url=ICON_URL,
            tags=[
                "research brief", "cited synthesis", "multi-source report",
                "delegate research", "sourced report", "async research",
            ],
            extensions=declare_discovery_extension(
                input={"subject": "Example Corp"},
                input_schema=JOBS_INPUT_SCHEMA,
                body_type="json",
                output=OutputConfig(
                    example={"job_id": "abc123", "eta_seconds": 60},
                    schema=JOBS_OUTPUT_SCHEMA,
                ),
            ),
        ),
        "POST /pdf": RouteConfig(
            accepts=_payment_option(config.PRICE_PDF),
            resource=f"{config.BASE_URL}/pdf",
            description=ROUTE_DESCRIPTIONS["pdf"],
            mime_type="application/json",
            service_name="pdf-to-markdown",
            icon_url=ICON_URL,
            tags=["pdf", "documents", "markdown"],
            extensions=declare_discovery_extension(
                input={"url": "https://www.ohchr.org/sites/default/files/UDHR/Documents/UDHR_Translations/eng.pdf"},
                input_schema=PDF_INPUT_SCHEMA,
                body_type="json",
                output=OutputConfig(example=PDF_SAMPLE_OUTPUT, schema=PDF_OUTPUT_SCHEMA),
            ),
        ),
        "POST /web-read": RouteConfig(
            accepts=_payment_option(config.PRICE_WEB_READ),
            resource=f"{config.BASE_URL}/web-read",
            description=ROUTE_DESCRIPTIONS["web-read"],
            mime_type="application/json",
            service_name="web-page-to-text",
            icon_url=ICON_URL,
            tags=["web", "scraping", "markdown"],
            extensions=declare_discovery_extension(
                input={"url": "https://www.w3.org/"},
                input_schema=WEB_READ_INPUT_SCHEMA,
                body_type="json",
                output=OutputConfig(example=WEB_READ_SAMPLE_OUTPUT, schema=WEB_READ_OUTPUT_SCHEMA),
            ),
        ),
        "POST /extract": RouteConfig(
            accepts=_payment_option(config.PRICE_EXTRACT),
            resource=f"{config.BASE_URL}/extract",
            description=ROUTE_DESCRIPTIONS["extract"],
            mime_type="application/json",
            service_name="extract-structured-data",
            icon_url=ICON_URL,
            tags=["extract", "structured data", "json schema"],
            extensions=declare_discovery_extension(
                input={
                    "text": "Widget Pro is our flagship gadget, priced at $29.99 and currently in stock.",
                    "schema": {
                        "type": "object",
                        "properties": {
                            "product_name": {"type": "string"},
                            "price": {"type": "number"},
                            "in_stock": {"type": "boolean"},
                        },
                    },
                },
                input_schema=EXTRACT_INPUT_SCHEMA,
                body_type="json",
                output=OutputConfig(example=EXTRACT_SAMPLE_OUTPUT, schema=EXTRACT_OUTPUT_SCHEMA),
            ),
        ),
        "POST /summarize": RouteConfig(
            accepts=_payment_option(config.PRICE_SUMMARIZE),
            resource=f"{config.BASE_URL}/summarize",
            description=ROUTE_DESCRIPTIONS["summarize"],
            mime_type="application/json",
            service_name="summarize-text",
            icon_url=ICON_URL,
            tags=["summarize", "tldr", "summarization"],
            extensions=declare_discovery_extension(
                input={"url": "https://en.wikipedia.org/wiki/Photosynthesis", "length": "short"},
                input_schema=SUMMARIZE_INPUT_SCHEMA,
                body_type="json",
                output=OutputConfig(example=SUMMARIZE_SAMPLE_OUTPUT, schema=SUMMARIZE_OUTPUT_SCHEMA),
            ),
        ),
        # /fact-check reactive le 2026-09-11, meme cause que /search
        # ci-dessus : _check_claim() (app/handlers/fact_check.py) est deja
        # sur SearXNG + gemma3:4b local depuis le 2026-09-11, la route
        # repond 200 en direct sur /fact-check/sample, mais restait absente
        # du challenge x402/well-known/capabilities/MCP - donc invisible aux
        # acheteurs.
        "POST /fact-check": RouteConfig(
            accepts=_payment_option(config.PRICE_FACT_CHECK),
            resource=f"{config.BASE_URL}/fact-check",
            description=ROUTE_DESCRIPTIONS["fact-check"],
            mime_type="application/json",
            service_name="fact-check",
            icon_url=ICON_URL,
            tags=["fact check", "verification", "claim"],
            extensions=declare_discovery_extension(
                input={"claim": "The Eiffel Tower is taller than the Statue of Liberty."},
                input_schema=FACT_CHECK_INPUT_SCHEMA,
                body_type="json",
                output=OutputConfig(example=FACT_CHECK_SAMPLE_OUTPUT, schema=FACT_CHECK_OUTPUT_SCHEMA),
            ),
        ),
        "POST /discover": RouteConfig(
            accepts=_payment_option(config.PRICE_DISCOVER),
            resource=f"{config.BASE_URL}/discover",
            description=ROUTE_DESCRIPTIONS["discover"],
            mime_type="application/json",
            service_name="mcp-discovery",
            icon_url=ICON_URL,
            tags=["mcp", "discovery"],
            extensions=declare_discovery_extension(
                input={"q": "read a PDF and give me markdown"},
                input_schema=DISCOVER_INPUT_SCHEMA,
                body_type="json",
                output=OutputConfig(example=DISCOVER_SAMPLE_OUTPUT, schema=DISCOVER_OUTPUT_SCHEMA),
            ),
        ),
        "POST /weather": RouteConfig(
            accepts=_payment_option(config.PRICE_WEATHER),
            resource=f"{config.BASE_URL}/weather",
            description=ROUTE_DESCRIPTIONS["weather"],
            mime_type="application/json",
            service_name="weather-forecast",
            icon_url=ICON_URL,
            tags=["weather", "forecast", "temperature", "climate", "open data"],
            extensions=declare_discovery_extension(
                input={"city": "Paris"},
                input_schema=WEATHER_INPUT_SCHEMA,
                body_type="json",
                output=OutputConfig(example=WEATHER_SAMPLE_OUTPUT, schema=WEATHER_OUTPUT_SCHEMA),
            ),
        ),
        # GET twin — paying agents (shizu-style) hit query-string paywalls first.
        "GET /weather": RouteConfig(
            accepts=_payment_option(config.PRICE_WEATHER),
            resource=f"{config.BASE_URL}/weather",
            description=ROUTE_DESCRIPTIONS["weather"],
            mime_type="application/json",
            service_name="weather-forecast",
            icon_url=ICON_URL,
            tags=["weather", "forecast", "temperature", "climate", "open data"],
            extensions=declare_discovery_extension(
                input={"city": "Paris"},
                input_schema=WEATHER_INPUT_SCHEMA,
                output=OutputConfig(example=WEATHER_SAMPLE_OUTPUT, schema=WEATHER_OUTPUT_SCHEMA),
            ),
        ),
        "POST /crypto": RouteConfig(
            accepts=_payment_option(config.PRICE_CRYPTO),
            resource=f"{config.BASE_URL}/crypto",
            description=ROUTE_DESCRIPTIONS["crypto"],
            mime_type="application/json",
            service_name="crypto-price",
            icon_url=ICON_URL,
            tags=["crypto", "price", "market data"],
            extensions=declare_discovery_extension(
                input={"coins": ["btc", "eth"], "vs_currency": "usd"},
                input_schema=CRYPTO_INPUT_SCHEMA,
                body_type="json",
                output=OutputConfig(example=CRYPTO_SAMPLE_OUTPUT, schema=CRYPTO_OUTPUT_SCHEMA),
            ),
        ),
        "GET /crypto": RouteConfig(
            accepts=_payment_option(config.PRICE_CRYPTO),
            resource=f"{config.BASE_URL}/crypto",
            description=ROUTE_DESCRIPTIONS["crypto"],
            mime_type="application/json",
            service_name="crypto-price",
            icon_url=ICON_URL,
            tags=["crypto", "price", "market data"],
            extensions=declare_discovery_extension(
                input={"coins": "btc,eth", "vs_currency": "usd"},
                input_schema=CRYPTO_INPUT_SCHEMA,
                output=OutputConfig(example=CRYPTO_SAMPLE_OUTPUT, schema=CRYPTO_OUTPUT_SCHEMA),
            ),
        ),
        "POST /news": RouteConfig(
            accepts=_payment_option(config.PRICE_NEWS),
            resource=f"{config.BASE_URL}/news",
            description=ROUTE_DESCRIPTIONS["news"],
            mime_type="application/json",
            service_name="tech-news-headlines",
            icon_url=ICON_URL,
            tags=["news", "headlines", "hacker news", "tech news", "trending"],
            extensions=declare_discovery_extension(
                input={"limit": 10},
                input_schema=NEWS_INPUT_SCHEMA,
                body_type="json",
                output=OutputConfig(example=NEWS_SAMPLE_OUTPUT, schema=NEWS_OUTPUT_SCHEMA),
            ),
        ),
        "GET /news": RouteConfig(
            accepts=_payment_option(config.PRICE_NEWS),
            resource=f"{config.BASE_URL}/news",
            description=ROUTE_DESCRIPTIONS["news"],
            mime_type="application/json",
            service_name="tech-news-headlines",
            icon_url=ICON_URL,
            tags=["news", "headlines", "hacker news", "tech news", "trending"],
            extensions=declare_discovery_extension(
                input={"limit": 10},
                input_schema=NEWS_INPUT_SCHEMA,
                output=OutputConfig(example=NEWS_SAMPLE_OUTPUT, schema=NEWS_OUTPUT_SCHEMA),
            ),
        ),
        "POST /can-pay": RouteConfig(
            accepts=_payment_option(config.PRICE_CAN_PAY),
            resource=f"{config.BASE_URL}/can-pay",
            description=ROUTE_DESCRIPTIONS["can-pay"],
            mime_type="application/json",
            service_name="wallet-can-pay",
            icon_url=ICON_URL,
            tags=["wallet", "usdc", "can pay", "preflight"],
            extensions=declare_discovery_extension(
                input={"address": "0xb3F32bdfe8D07825BC0D7387295aB1D7559BA69d", "amount": 0.001},
                input_schema=CAN_PAY_INPUT_SCHEMA,
                body_type="json",
                output=OutputConfig(example=CAN_PAY_SAMPLE_OUTPUT, schema=CAN_PAY_OUTPUT_SCHEMA),
            ),
        ),
        "GET /can-pay": RouteConfig(
            accepts=_payment_option(config.PRICE_CAN_PAY),
            resource=f"{config.BASE_URL}/can-pay",
            description=ROUTE_DESCRIPTIONS["can-pay"],
            mime_type="application/json",
            service_name="wallet-can-pay",
            icon_url=ICON_URL,
            tags=["wallet", "usdc", "can pay", "preflight"],
            extensions=declare_discovery_extension(
                input={"address": "0xb3F32bdfe8D07825BC0D7387295aB1D7559BA69d", "amount": 0.001},
                input_schema=CAN_PAY_INPUT_SCHEMA,
                output=OutputConfig(example=CAN_PAY_SAMPLE_OUTPUT, schema=CAN_PAY_OUTPUT_SCHEMA),
            ),
        ),
        "POST /probe": RouteConfig(
            accepts=_payment_option(config.PRICE_PROBE),
            resource=f"{config.BASE_URL}/probe",
            description=ROUTE_DESCRIPTIONS["probe"],
            mime_type="application/json",
            service_name="x402-paywall-probe",
            icon_url=ICON_URL,
            tags=["x402", "probe", "discovery", "paywall", "price check"],
            extensions=declare_discovery_extension(
                input={"url": "https://x402.shizu.me/weather?lat=48.85&lon=2.35"},
                input_schema=PROBE_INPUT_SCHEMA,
                body_type="json",
                output=OutputConfig(example=PROBE_SAMPLE_OUTPUT, schema=PROBE_OUTPUT_SCHEMA),
            ),
        ),
        "GET /probe": RouteConfig(
            accepts=_payment_option(config.PRICE_PROBE),
            resource=f"{config.BASE_URL}/probe",
            description=ROUTE_DESCRIPTIONS["probe"],
            mime_type="application/json",
            service_name="x402-paywall-probe",
            icon_url=ICON_URL,
            tags=["x402", "probe", "discovery", "paywall", "price check"],
            extensions=declare_discovery_extension(
                input={"url": "https://x402.shizu.me/weather?lat=48.85&lon=2.35"},
                input_schema=PROBE_INPUT_SCHEMA,
                output=OutputConfig(example=PROBE_SAMPLE_OUTPUT, schema=PROBE_OUTPUT_SCHEMA),
            ),
        ),
        "POST /wallet-balance": RouteConfig(
            accepts=_payment_option(config.PRICE_WALLET_BALANCE),
            resource=f"{config.BASE_URL}/wallet-balance",
            description=ROUTE_DESCRIPTIONS["wallet-balance"],
            mime_type="application/json",
            service_name="wallet-balance-checker",
            icon_url=ICON_URL,
            tags=["wallet", "balance", "USDC"],
            extensions=declare_discovery_extension(
                input={
                    "address": "0xb3F32bdfe8D07825BC0D7387295aB1D7559BA69d",
                    "network": "base",
                },
                input_schema=WALLET_BALANCE_INPUT_SCHEMA,
                body_type="json",
                output=OutputConfig(
                    example=WALLET_BALANCE_SAMPLE_OUTPUT,
                    schema=WALLET_BALANCE_OUTPUT_SCHEMA,
                ),
            ),
        ),
        "GET /wallet-balance": RouteConfig(
            accepts=_payment_option(config.PRICE_WALLET_BALANCE),
            resource=f"{config.BASE_URL}/wallet-balance",
            description=ROUTE_DESCRIPTIONS["wallet-balance"],
            mime_type="application/json",
            service_name="wallet-balance-checker",
            icon_url=ICON_URL,
            tags=["wallet", "balance", "USDC"],
            extensions=declare_discovery_extension(
                input={
                    "address": "0xb3F32bdfe8D07825BC0D7387295aB1D7559BA69d",
                    "network": "base",
                },
                input_schema=WALLET_BALANCE_INPUT_SCHEMA,
                output=OutputConfig(
                    example=WALLET_BALANCE_SAMPLE_OUTPUT,
                    schema=WALLET_BALANCE_OUTPUT_SCHEMA,
                ),
            ),
        ),
        "POST /gas-price": RouteConfig(
            accepts=_payment_option(config.PRICE_GAS_PRICE),
            resource=f"{config.BASE_URL}/gas-price",
            description=ROUTE_DESCRIPTIONS["gas-price"],
            mime_type="application/json",
            service_name="gas-price-checker",
            icon_url=ICON_URL,
            tags=["gas", "fee", "EVM"],
            extensions=declare_discovery_extension(
                input={"network": "base"},
                input_schema=GAS_PRICE_INPUT_SCHEMA,
                body_type="json",
                output=OutputConfig(
                    example=GAS_PRICE_SAMPLE_OUTPUT,
                    schema=GAS_PRICE_OUTPUT_SCHEMA,
                ),
            ),
        ),
        "GET /gas-price": RouteConfig(
            accepts=_payment_option(config.PRICE_GAS_PRICE),
            resource=f"{config.BASE_URL}/gas-price",
            description=ROUTE_DESCRIPTIONS["gas-price"],
            mime_type="application/json",
            service_name="gas-price-checker",
            icon_url=ICON_URL,
            tags=["gas", "fee", "EVM"],
            extensions=declare_discovery_extension(
                input={"network": "base"},
                input_schema=GAS_PRICE_INPUT_SCHEMA,
                output=OutputConfig(
                    example=GAS_PRICE_SAMPLE_OUTPUT,
                    schema=GAS_PRICE_OUTPUT_SCHEMA,
                ),
            ),
        ),
        "POST /wallet-intelligence": RouteConfig(
            accepts=_payment_option(config.PRICE_WALLET_INTELLIGENCE),
            resource=f"{config.BASE_URL}/wallet-intelligence",
            description=ROUTE_DESCRIPTIONS["wallet-intelligence"],
            mime_type="application/json",
            service_name="wallet-and-gas-lookup",
            icon_url=ICON_URL,
            tags=["wallet intelligence", "multi-chain", "gas"],
            extensions=declare_discovery_extension(
                input={
                    "address": "0xb3F32bdfe8D07825BC0D7387295aB1D7559BA69d",
                    "networks": ["base", "ethereum", "polygon", "arbitrum", "optimism"],
                    "amount_usdc": 0.001,
                },
                input_schema=WALLET_INTELLIGENCE_INPUT_SCHEMA,
                body_type="json",
                output=OutputConfig(
                    example=WALLET_INTELLIGENCE_SAMPLE_OUTPUT,
                    schema=WALLET_INTELLIGENCE_OUTPUT_SCHEMA,
                ),
            ),
        ),
        "GET /wallet-intelligence": RouteConfig(
            accepts=_payment_option(config.PRICE_WALLET_INTELLIGENCE),
            resource=f"{config.BASE_URL}/wallet-intelligence",
            description=ROUTE_DESCRIPTIONS["wallet-intelligence"],
            mime_type="application/json",
            service_name="wallet-and-gas-lookup",
            icon_url=ICON_URL,
            tags=["wallet intelligence", "multi-chain", "gas"],
            extensions=declare_discovery_extension(
                input={
                    "address": "0xb3F32bdfe8D07825BC0D7387295aB1D7559BA69d",
                    "networks": "base,ethereum,polygon,arbitrum,optimism",
                    "amount_usdc": 0.001,
                },
                input_schema=WALLET_INTELLIGENCE_INPUT_SCHEMA,
                output=OutputConfig(
                    example=WALLET_INTELLIGENCE_SAMPLE_OUTPUT,
                    schema=WALLET_INTELLIGENCE_OUTPUT_SCHEMA,
                ),
            ),
        ),
        "POST /x402-echo": RouteConfig(
            accepts=PaymentOption(
                scheme="exact",
                pay_to=config.X402_ECHO_PAY_TO,
                price=config.PRICE_X402_ECHO,
                network=config.X402_NETWORK,
            ),
            resource=f"{config.BASE_URL}/x402-echo",
            description=ROUTE_DESCRIPTIONS["x402-echo"],
            mime_type="application/json",
            service_name="x402-payment-test",
            icon_url=ICON_URL,
            tags=["x402", "conformance", "echo"],
            extensions=declare_discovery_extension(
                input={"message": "hello agent"},
                input_schema=X402_ECHO_INPUT_SCHEMA,
                body_type="json",
                output=OutputConfig(
                    example=X402_ECHO_SAMPLE_OUTPUT,
                    schema=X402_ECHO_OUTPUT_SCHEMA,
                ),
            ),
        ),
        "GET /x402-echo": RouteConfig(
            accepts=PaymentOption(
                scheme="exact",
                pay_to=config.X402_ECHO_PAY_TO,
                price=config.PRICE_X402_ECHO,
                network=config.X402_NETWORK,
            ),
            resource=f"{config.BASE_URL}/x402-echo",
            description=ROUTE_DESCRIPTIONS["x402-echo"],
            mime_type="application/json",
            service_name="x402-payment-test",
            icon_url=ICON_URL,
            tags=["x402", "conformance", "echo"],
            extensions=declare_discovery_extension(
                input={"message": "hello agent"},
                input_schema=X402_ECHO_INPUT_SCHEMA,
                output=OutputConfig(
                    example=X402_ECHO_SAMPLE_OUTPUT,
                    schema=X402_ECHO_OUTPUT_SCHEMA,
                ),
            ),
        ),
        "POST /tip": RouteConfig(
            accepts=_payment_option(config.PRICE_TIP),
            resource=f"{config.BASE_URL}/tip",
            description=ROUTE_DESCRIPTIONS["tip"],
            mime_type="application/json",
            service_name="usdc-tip",
            icon_url=ICON_URL,
            tags=["tip", "support", "sponsor", "USDC", "agent infrastructure"],
            extensions=declare_discovery_extension(
                input={"message": "Keep building agent infrastructure"},
                input_schema=X402_ECHO_INPUT_SCHEMA,
                body_type="json",
                output=OutputConfig(
                    example={
                        **X402_ECHO_SAMPLE_OUTPUT,
                        "purpose": "support-agentindex",
                        "price_usdc": 0.01,
                        "price_atomic_usdc": "10000",
                    },
                    schema=X402_ECHO_OUTPUT_SCHEMA,
                ),
            ),
        ),
        "GET /tip": RouteConfig(
            accepts=_payment_option(config.PRICE_TIP),
            resource=f"{config.BASE_URL}/tip",
            description=ROUTE_DESCRIPTIONS["tip"],
            mime_type="application/json",
            service_name="usdc-tip",
            icon_url=ICON_URL,
            tags=["tip", "support", "sponsor", "USDC", "agent infrastructure"],
            extensions=declare_discovery_extension(
                input={"message": "Keep building agent infrastructure"},
                input_schema=X402_ECHO_INPUT_SCHEMA,
                output=OutputConfig(
                    example={
                        **X402_ECHO_SAMPLE_OUTPUT,
                        "purpose": "support-agentindex",
                        "price_usdc": 0.01,
                        "price_atomic_usdc": "10000",
                    },
                    schema=X402_ECHO_OUTPUT_SCHEMA,
                ),
            ),
        ),
        "POST /agent-claim": RouteConfig(
            accepts=_payment_option(config.PRICE_AGENT_CLAIM),
            resource=f"{config.BASE_URL}/agent-claim",
            description=ROUTE_DESCRIPTIONS["agent-claim"],
            mime_type="application/json",
            service_name="agent-verification-listing",
            icon_url=ICON_URL,
            tags=["agent registry", "verification badge", "discovery", "trust", "x402"],
            extensions=declare_discovery_extension(
                input={"url": "https://x402.agentindex.world", "name": "AgentIndex x402"},
                input_schema={
                    "type": "object",
                    "required": ["url"],
                    "properties": {
                        "url": {"type": "string", "description": "Public agent or API URL"},
                        "name": {"type": "string", "description": "Public agent name"},
                        "method": {"type": "string", "enum": ["GET", "POST", "PUT", "HEAD"]},
                    },
                },
                body_type="json",
                output=OutputConfig(
                    example={
                        "id": "8e6c3b7ef184b00c3a21",
                        "url": "https://x402.agentindex.world",
                        "score": 95,
                        "verdict": "operational",
                        "verified_at": "2026-09-20T02:20:00Z",
                        "expires_at": "2026-10-20T02:20:00Z",
                        "listing": f"{config.BASE_URL}/verified-agents.json",
                        "badge": f"{config.BASE_URL}/verified-agents/8e6c3b7ef184b00c3a21.svg",
                    },
                    schema={
                        "type": "object",
                        "required": ["id", "url", "score", "verdict", "expires_at", "listing", "badge"],
                        "properties": {
                            "id": {"type": "string"},
                            "url": {"type": "string"},
                            "score": {"type": "integer"},
                            "verdict": {"type": "string"},
                            "expires_at": {"type": "string"},
                            "listing": {"type": "string"},
                            "badge": {"type": "string"},
                        },
                    },
                ),
            ),
        ),
        "POST /agent-health": RouteConfig(
            accepts=_payment_option(config.PRICE_AGENT_HEALTH),
            resource=f"{config.BASE_URL}/agent-health",
            description=ROUTE_DESCRIPTIONS["agent-health"],
            mime_type="application/json",
            service_name="api-health-check",
            icon_url=ICON_URL,
            tags=["agent health", "monitoring", "uptime", "x402 audit"],
            extensions=declare_discovery_extension(
                input={
                    "url": "https://x402.agentindex.world/search",
                    "method": "POST",
                    "body": {"query": "test"},
                },
                input_schema=AGENT_HEALTH_INPUT_SCHEMA,
                body_type="json",
                output=OutputConfig(
                    example=AGENT_HEALTH_SAMPLE_OUTPUT,
                    schema=AGENT_HEALTH_OUTPUT_SCHEMA,
                ),
            ),
        ),
        "GET /agent-health": RouteConfig(
            accepts=_payment_option(config.PRICE_AGENT_HEALTH),
            resource=f"{config.BASE_URL}/agent-health",
            description=ROUTE_DESCRIPTIONS["agent-health"],
            mime_type="application/json",
            service_name="api-health-check",
            icon_url=ICON_URL,
            tags=["agent health", "monitoring", "uptime", "x402 audit"],
            extensions=declare_discovery_extension(
                input={"url": "https://x402.agentindex.world/search", "method": "POST"},
                input_schema=AGENT_HEALTH_INPUT_SCHEMA,
                output=OutputConfig(
                    example=AGENT_HEALTH_SAMPLE_OUTPUT,
                    schema=AGENT_HEALTH_OUTPUT_SCHEMA,
                ),
            ),
        ),
        "POST /decide": RouteConfig(
            accepts=_payment_option(config.PRICE_DECIDE),
            resource=f"{config.BASE_URL}/decide",
            description=ROUTE_DESCRIPTIONS["decide"],
            mime_type="application/json",
            service_name="decision-questions",
            icon_url=ICON_URL,
            tags=["decision", "classification"],
            extensions=declare_discovery_extension(
                input=DECIDE_SAMPLE_INPUT,
                input_schema=DECIDE_INPUT_SCHEMA,
                body_type="json",
                output=OutputConfig(example=DECIDE_SAMPLE_OUTPUT, schema=DECIDE_OUTPUT_SCHEMA),
            ),
        ),
        "POST /guard": RouteConfig(
            accepts=_payment_option(config.PRICE_GUARD),
            resource=f"{config.BASE_URL}/guard",
            description=ROUTE_DESCRIPTIONS["guard"],
            mime_type="application/json",
            service_name="tool-call-guard",
            icon_url=ICON_URL,
            tags=["guardrail"],
            extensions=declare_discovery_extension(
                input=GUARD_SAMPLE_INPUT,
                input_schema=GUARD_INPUT_SCHEMA,
                body_type="json",
                output=OutputConfig(example=GUARD_SAMPLE_OUTPUT, schema=GUARD_OUTPUT_SCHEMA),
            ),
        ),
        "POST /verify": RouteConfig(
            accepts=_payment_option(config.PRICE_VERIFY),
            resource=f"{config.BASE_URL}/verify",
            description=ROUTE_DESCRIPTIONS["verify"],
            mime_type="application/json",
            service_name="claim-verification",
            icon_url=ICON_URL,
            tags=["verification"],
            extensions=declare_discovery_extension(
                input=VERIFY_SAMPLE_INPUT,
                input_schema=VERIFY_INPUT_SCHEMA,
                body_type="json",
                output=OutputConfig(example=VERIFY_SAMPLE_OUTPUT, schema=VERIFY_OUTPUT_SCHEMA),
            ),
        ),
        "POST /rank": RouteConfig(
            accepts=_payment_option(config.PRICE_RANK),
            resource=f"{config.BASE_URL}/rank",
            description=ROUTE_DESCRIPTIONS["rank"],
            mime_type="application/json",
            service_name="document-rerank",
            icon_url=ICON_URL,
            tags=["rerank"],
            extensions=declare_discovery_extension(
                input=RANK_SAMPLE_INPUT,
                input_schema=RANK_INPUT_SCHEMA,
                body_type="json",
                output=OutputConfig(example=RANK_SAMPLE_OUTPUT, schema=RANK_OUTPUT_SCHEMA),
            ),
        ),
        "POST /token-risk": RouteConfig(
            accepts=_payment_option(config.PRICE_TOKEN_RISK),
            resource=f"{config.BASE_URL}/token-risk",
            description=ROUTE_DESCRIPTIONS["token-risk"],
            mime_type="application/json",
            service_name="token-risk-scan",
            icon_url=ICON_URL,
            tags=["token risk", "rug check", "honeypot", "Base token safety"],
            extensions=declare_discovery_extension(
                input=TOKEN_RISK_SAMPLE_INPUT,
                input_schema=TOKEN_RISK_INPUT_SCHEMA,
                body_type="json",
                output=OutputConfig(example=TOKEN_RISK_SAMPLE_OUTPUT, schema=TOKEN_RISK_OUTPUT_SCHEMA),
            ),
        ),
        "GET /token-card": RouteConfig(
            accepts=_payment_option(config.PRICE_TOKEN_CARD),
            resource=f"{config.BASE_URL}/token-card",
            description=ROUTE_DESCRIPTIONS["token-card"],
            mime_type="application/json",
            service_name="token-verdict-card",
            icon_url=ICON_URL,
            tags=TOKEN_CARD_TAGS,
            extensions=declare_discovery_extension(
                input=TOKEN_CARD_SAMPLE_INPUT,
                input_schema=TOKEN_CARD_INPUT_SCHEMA,
                output=OutputConfig(example=TOKEN_CARD_SAMPLE_OUTPUT, schema=TOKEN_CARD_OUTPUT_SCHEMA),
            ),
        ),
        "POST /token-card": RouteConfig(
            accepts=_payment_option(config.PRICE_TOKEN_CARD),
            resource=f"{config.BASE_URL}/token-card",
            description=ROUTE_DESCRIPTIONS["token-card"],
            mime_type="application/json",
            service_name="token-verdict-card",
            icon_url=ICON_URL,
            tags=TOKEN_CARD_TAGS,
            extensions=declare_discovery_extension(
                input=TOKEN_CARD_SAMPLE_INPUT,
                input_schema=TOKEN_CARD_INPUT_SCHEMA,
                body_type="json",
                output=OutputConfig(example=TOKEN_CARD_SAMPLE_OUTPUT, schema=TOKEN_CARD_OUTPUT_SCHEMA),
            ),
        ),
        "POST /research": RouteConfig(
            accepts=_payment_option(config.PRICE_RESEARCH),
            resource=f"{config.BASE_URL}/research",
            description=ROUTE_DESCRIPTIONS["research"],
            mime_type="application/json",
            service_name="cited-research",
            icon_url=ICON_URL,
            tags=["research report with sources", "cited answer", "web research"],
            extensions=declare_discovery_extension(
                input=RESEARCH_SAMPLE_INPUT,
                input_schema=RESEARCH_INPUT_SCHEMA,
                body_type="json",
                output=OutputConfig(example=RESEARCH_SAMPLE_OUTPUT, schema=RESEARCH_OUTPUT_SCHEMA),
            ),
        ),
        "POST /v1/chat/completions": RouteConfig(
            # "exact" only while UPTO_ENABLED is False (app/handlers/llm_gateway.py
            # module docstring has the full story: the route never appeared
            # in Bazaar even an hour after a real, confirmed on-chain
            # settlement, and offering a second, dynamically-priced, non-exact
            # option was the only structural difference from every other
            # route that does get indexed). _llm_gateway_accepts is built
            # above from that same flag, so flipping it back on needs no
            # change here.
            accepts=_llm_gateway_accepts,
            resource=f"{config.BASE_URL}/v1/chat/completions",
            description=ROUTE_DESCRIPTIONS["llm-gateway"],
            mime_type="application/json",
            service_name="chat-completions-api",
            icon_url=ICON_URL,
            tags=["chat completions", "ai inference", "llm api", "pay per token llm", "openai compatible"],
            extensions=declare_discovery_extension(
                input=SAMPLE_REQUEST,
                input_schema=LLM_GATEWAY_INPUT_SCHEMA,
                body_type="json",
                output=OutputConfig(example=SAMPLE_RESPONSE, schema=LLM_GATEWAY_OUTPUT_SCHEMA),
            ),
        ),
        "POST /sentiment": RouteConfig(
            accepts=_payment_option(config.PRICE_SENTIMENT),
            resource=f"{config.BASE_URL}/sentiment",
            description=ROUTE_DESCRIPTIONS["sentiment"],
            mime_type="application/json",
            service_name="sentiment-analysis",
            icon_url=ICON_URL,
            tags=["sentiment analysis", "text sentiment", "opinion mining", "Powered by Jev"],
            extensions=declare_discovery_extension(
                input=SENTIMENT_SAMPLE_INPUT,
                input_schema=SENTIMENT_INPUT_SCHEMA,
                body_type="json",
                output=OutputConfig(example=SENTIMENT_SAMPLE_OUTPUT, schema=SENTIMENT_OUTPUT_SCHEMA),
            ),
        ),
        "POST /classify": RouteConfig(
            accepts=_payment_option(config.PRICE_CLASSIFY),
            resource=f"{config.BASE_URL}/classify",
            description=ROUTE_DESCRIPTIONS["classify"],
            mime_type="application/json",
            service_name="support-ticket-classifier",
            icon_url=ICON_URL,
            tags=["classify text", "text classification", "custom labels", "categorize support ticket", "Powered by Jev"],
            extensions=declare_discovery_extension(
                input=CLASSIFY_SAMPLE_INPUT,
                input_schema=CLASSIFY_INPUT_SCHEMA,
                body_type="json",
                output=OutputConfig(example=CLASSIFY_SAMPLE_OUTPUT, schema=CLASSIFY_OUTPUT_SCHEMA),
            ),
        ),
        "POST /intent": RouteConfig(
            accepts=_payment_option(config.PRICE_INTENT),
            resource=f"{config.BASE_URL}/intent",
            description=ROUTE_DESCRIPTIONS["intent"],
            mime_type="application/json",
            service_name="intent-detection",
            icon_url=ICON_URL,
            tags=["intent detection", "intent classification", "text intent", "Powered by Jev"],
            extensions=declare_discovery_extension(
                input=INTENT_SAMPLE_INPUT,
                input_schema=INTENT_INPUT_SCHEMA,
                body_type="json",
                output=OutputConfig(example=INTENT_SAMPLE_OUTPUT, schema=INTENT_OUTPUT_SCHEMA),
            ),
        ),
        "POST /spam-check": RouteConfig(
            accepts=_payment_option(config.PRICE_SPAM_CHECK),
            resource=f"{config.BASE_URL}/spam-check",
            description=ROUTE_DESCRIPTIONS["spam-check"],
            mime_type="application/json",
            service_name="spam-detection",
            icon_url=ICON_URL,
            tags=["spam detection", "spam filter", "spam classifier", "Powered by Jev"],
            extensions=declare_discovery_extension(
                input=SPAM_CHECK_SAMPLE_INPUT,
                input_schema=SPAM_CHECK_INPUT_SCHEMA,
                body_type="json",
                output=OutputConfig(example=SPAM_CHECK_SAMPLE_OUTPUT, schema=SPAM_CHECK_OUTPUT_SCHEMA),
            ),
        ),
        "POST /toxicity": RouteConfig(
            accepts=_payment_option(config.PRICE_TOXICITY),
            resource=f"{config.BASE_URL}/toxicity",
            description=ROUTE_DESCRIPTIONS["toxicity"],
            mime_type="application/json",
            service_name="toxicity-detection",
            icon_url=ICON_URL,
            tags=["toxicity detection", "content moderation", "hate speech detection", "Powered by Jev"],
            extensions=declare_discovery_extension(
                input=TOXICITY_SAMPLE_INPUT,
                input_schema=TOXICITY_INPUT_SCHEMA,
                body_type="json",
                output=OutputConfig(example=TOXICITY_SAMPLE_OUTPUT, schema=TOXICITY_OUTPUT_SCHEMA),
            ),
        ),
        "POST /language": RouteConfig(
            accepts=_payment_option(config.PRICE_LANGUAGE),
            resource=f"{config.BASE_URL}/language",
            description=ROUTE_DESCRIPTIONS["language"],
            mime_type="application/json",
            service_name="language-detection",
            icon_url=ICON_URL,
            tags=["language detection", "language identification", "detect language", "Powered by Jev"],
            extensions=declare_discovery_extension(
                input=LANGUAGE_SAMPLE_INPUT,
                input_schema=LANGUAGE_INPUT_SCHEMA,
                body_type="json",
                output=OutputConfig(example=LANGUAGE_SAMPLE_OUTPUT, schema=LANGUAGE_OUTPUT_SCHEMA),
            ),
        ),
        "POST /pii-check": RouteConfig(
            accepts=_payment_option(config.PRICE_PII_CHECK),
            resource=f"{config.BASE_URL}/pii-check",
            description=ROUTE_DESCRIPTIONS["pii-check"],
            mime_type="application/json",
            service_name="pii-detection",
            icon_url=ICON_URL,
            tags=["PII detection", "PII scanner", "personal data detection", "Powered by Jev"],
            extensions=declare_discovery_extension(
                input=PII_CHECK_SAMPLE_INPUT,
                input_schema=PII_CHECK_INPUT_SCHEMA,
                body_type="json",
                output=OutputConfig(example=PII_CHECK_SAMPLE_OUTPUT, schema=PII_CHECK_OUTPUT_SCHEMA),
            ),
        ),
    }
    if not RESEARCH_ENABLED:
        _routes.pop("POST /research", None)
    if not SENTIMENT_ENABLED:
        _routes.pop("POST /sentiment", None)
    if not CLASSIFY_ENABLED:
        _routes.pop("POST /classify", None)
    if not INTENT_ENABLED:
        _routes.pop("POST /intent", None)
    if not SPAM_CHECK_ENABLED:
        _routes.pop("POST /spam-check", None)
    if not TOXICITY_ENABLED:
        _routes.pop("POST /toxicity", None)
    if not LANGUAGE_ENABLED:
        _routes.pop("POST /language", None)
    if not PII_CHECK_ENABLED:
        _routes.pop("POST /pii-check", None)

    # Pack 2 (2026-09-30): 5 pinned per-model LLM routes, same engine as
    # POST /v1/chat/completions, each with its own fixed model+provider pin
    # and its own real captured /sample (single source of truth - the
    # sample IS what GET /*/sample serves, not a separate hand-maintained
    # copy - see the /token-risk and /weather duplicate-sample bug found
    # and fixed in the 2026-09-30 conformity batch).
    from app.handlers.llm_claude_sonnet import price_fn as _claude_sonnet_price_fn, SAMPLE_REQUEST as _CLAUDE_SONNET_SAMPLE_REQUEST, SAMPLE_RESPONSE as _CLAUDE_SONNET_SAMPLE_RESPONSE
    from app.handlers.llm_gpt_mini import price_fn as _gpt_mini_price_fn, SAMPLE_REQUEST as _GPT_MINI_SAMPLE_REQUEST, SAMPLE_RESPONSE as _GPT_MINI_SAMPLE_RESPONSE
    from app.handlers.llm_gemini_flash import GEMINI_FLASH_ENABLED, price_fn as _gemini_flash_price_fn, SAMPLE_REQUEST as _GEMINI_FLASH_SAMPLE_REQUEST, SAMPLE_RESPONSE as _GEMINI_FLASH_SAMPLE_RESPONSE
    from app.handlers.llm_llama import price_fn as _llama_price_fn, SAMPLE_REQUEST as _LLAMA_SAMPLE_REQUEST, SAMPLE_RESPONSE as _LLAMA_SAMPLE_RESPONSE
    from app.handlers.llm_deepseek import DEEPSEEK_ENABLED, price_fn as _deepseek_price_fn, SAMPLE_REQUEST as _DEEPSEEK_SAMPLE_REQUEST, SAMPLE_RESPONSE as _DEEPSEEK_SAMPLE_RESPONSE

    _routes["POST /llm/claude-sonnet"] = RouteConfig(
        accepts=_payment_option(_claude_sonnet_price_fn),
        resource=f"{config.BASE_URL}/llm/claude-sonnet",
        description=ROUTE_DESCRIPTIONS["llm-claude-sonnet"],
        mime_type="application/json",
        service_name="claude-sonnet-api",
        icon_url=ICON_URL,
        tags=["Claude Sonnet API", "claude api", "anthropic api", "pay per call llm"],
        extensions=declare_discovery_extension(
            input=_CLAUDE_SONNET_SAMPLE_REQUEST,
            input_schema=LLM_PER_MODEL_INPUT_SCHEMA,
            body_type="json",
            output=OutputConfig(example=_CLAUDE_SONNET_SAMPLE_RESPONSE, schema=LLM_GATEWAY_OUTPUT_SCHEMA),
        ),
    )
    _routes["POST /llm/gpt-mini"] = RouteConfig(
        accepts=_payment_option(_gpt_mini_price_fn),
        resource=f"{config.BASE_URL}/llm/gpt-mini",
        description=ROUTE_DESCRIPTIONS["llm-gpt-mini"],
        mime_type="application/json",
        service_name="gpt-mini-api",
        icon_url=ICON_URL,
        tags=["GPT Mini API", "gpt api", "openai api", "pay per call llm"],
        extensions=declare_discovery_extension(
            input=_GPT_MINI_SAMPLE_REQUEST,
            input_schema=LLM_PER_MODEL_INPUT_SCHEMA,
            body_type="json",
            output=OutputConfig(example=_GPT_MINI_SAMPLE_RESPONSE, schema=LLM_GATEWAY_OUTPUT_SCHEMA),
        ),
    )
    _routes["POST /llm/gemini-flash"] = RouteConfig(
        accepts=_payment_option(_gemini_flash_price_fn),
        resource=f"{config.BASE_URL}/llm/gemini-flash",
        description=ROUTE_DESCRIPTIONS["llm-gemini-flash"],
        mime_type="application/json",
        service_name="gemini-flash-api",
        icon_url=ICON_URL,
        tags=["Gemini Flash API", "gemini api", "google api", "pay per call llm"],
        extensions=declare_discovery_extension(
            input=_GEMINI_FLASH_SAMPLE_REQUEST,
            input_schema=LLM_PER_MODEL_INPUT_SCHEMA,
            body_type="json",
            output=OutputConfig(example=_GEMINI_FLASH_SAMPLE_RESPONSE, schema=LLM_GATEWAY_OUTPUT_SCHEMA),
        ),
    )
    _routes["POST /llm/llama"] = RouteConfig(
        accepts=_payment_option(_llama_price_fn),
        resource=f"{config.BASE_URL}/llm/llama",
        description=ROUTE_DESCRIPTIONS["llm-llama"],
        mime_type="application/json",
        service_name="llama-api",
        icon_url=ICON_URL,
        tags=["Llama API", "llama 4 api", "meta llama api", "pay per call llm"],
        extensions=declare_discovery_extension(
            input=_LLAMA_SAMPLE_REQUEST,
            input_schema=LLM_PER_MODEL_INPUT_SCHEMA,
            body_type="json",
            output=OutputConfig(example=_LLAMA_SAMPLE_RESPONSE, schema=LLM_GATEWAY_OUTPUT_SCHEMA),
        ),
    )
    _routes["POST /llm/deepseek"] = RouteConfig(
        accepts=_payment_option(_deepseek_price_fn),
        resource=f"{config.BASE_URL}/llm/deepseek",
        description=ROUTE_DESCRIPTIONS["llm-deepseek"],
        mime_type="application/json",
        service_name="deepseek-api",
        icon_url=ICON_URL,
        tags=["DeepSeek API", "deepseek v4 api", "pay per call llm"],
        extensions=declare_discovery_extension(
            input=_DEEPSEEK_SAMPLE_REQUEST,
            input_schema=LLM_PER_MODEL_INPUT_SCHEMA,
            body_type="json",
            output=OutputConfig(example=_DEEPSEEK_SAMPLE_RESPONSE, schema=LLM_GATEWAY_OUTPUT_SCHEMA),
        ),
    )
    if not GEMINI_FLASH_ENABLED:
        _routes.pop("POST /llm/gemini-flash", None)
    if not DEEPSEEK_ENABLED:
        _routes.pop("POST /llm/deepseek", None)

    # GET twins for POST-only Bazaar-listed routes (2026-10-01): AgentEconomyReport
    # (and presumably other GET-based probes, same precedent as the /weather GET
    # twin for "shizu-style" paying agents) sent an unpaid GET price-check to each
    # of these 13 routes and got FastAPI's 405 instead of our 402, since the
    # payment middleware only recognizes (method, path) pairs present in this
    # dict - a 405 isn't a declared payment challenge, so it read as downtime.
    # Real measured impact: 13 of ~23 probed routes affected, consistent with the
    # 46.9% availability figure that triggered the AgentEconomyReport D grade.
    # Config-only, no new FastAPI handler: the middleware answers unpaid GETs
    # with 402 directly, before ever reaching the router (confirmed by reading
    # x402/http/middleware/fastapi.py - requires_payment() short-circuits before
    # call_next()), so no regression for real buyers, who already use POST.
    for _get_twin_path in (
        # /token-card deliberately excluded (2026-10-10): it now has its own
        # real "GET /token-card" entry above (queryParams, own tags/output),
        # not a shared-object twin of the POST entry - this loop would
        # otherwise clobber it right back with the POST-body-shaped one.
        "/search", "/translate", "/pdf", "/web-read", "/extract", "/summarize",
        "/discover", "/decide", "/guard", "/verify", "/rank", "/token-risk",
        "/v1/chat/completions",
        # Same gap, found by auditing every Bazaar-listed resource rather than
        # just the 2 days of probe logs that caught the first 13 (2026-10-01).
        "/llm/llama", "/llm/gpt-mini", "/llm/claude-sonnet", "/pii-check",
        "/language", "/toxicity", "/spam-check", "/intent", "/classify",
        "/sentiment", "/research",
        # Caught by the first real run of scripts/probe_catalog_health.py
        # (2026-10-01) - listed in /.well-known/x402 but not yet in CDP
        # Bazaar's own index, so the earlier Bazaar-listing audit missed
        # them; same gap, same fix.
        "/agent-claim", "/fact-check", "/jobs",
    ):
        _post_key = f"POST {_get_twin_path}"
        if _post_key in _routes:
            _routes[f"GET {_get_twin_path}"] = _routes[_post_key]

    return _routes


async def _first_call_free_hook(context: SettleContext) -> SkipSettleResult | None:
    """x402 SDK before-settle hook (x402ResourceServer.on_before_settle), fired
    AFTER verify + AFTER the route handler already ran, for both HTTP
    (PaymentMiddlewareASGI) and MCP (app/mcp_server.py) - both share the one
    x402ResourceServer built here. Verification (real signature, real funds)
    already happened normally, so this never waives payment for an unfunded
    or invalid wallet - it only skips the actual facilitator settlement call
    (no USDC ever moves) the first time a given address is seen, globally
    across all 3 routes, then charges normally forever after.

    db.mark_wallet_seen() is the atomic check-and-record - only the call that
    genuinely inserts the row gets the free pass, so two concurrent first
    calls from the same wallet can't both slip through.

    Gated on config.FREE_FIRST_CALL (env FREE_FIRST_CALL) - off by default,
    see BRIEF-CORRECTIONS.md: a new wallet's first payment is exactly the
    settlement that triggers Bazaar CDP indexing and the only revenue
    automated probes produce, so waiving it is a real cost, not just a nice-
    to-have. When off, no wallet is ever recorded as seen either, so
    re-enabling later treats every wallet as genuinely new again.
    """
    if not config.FREE_FIRST_CALL:
        return None
    payload_dict = context.payment_payload.model_dump(by_alias=True, exclude_none=True)
    from_address = extract_payer_from_payment_dict(payload_dict)
    if not from_address:
        return None
    if not db.mark_wallet_seen(from_address):
        return None
    return SkipSettleResult(
        result=SettleResponse(
            success=True,
            payer=from_address,
            transaction="",
            network=str(context.requirements.network),
            amount="0",
        )
    )


def build_resource_server() -> x402ResourceServer:
    if config.CDP_API_KEY_ID and config.CDP_API_KEY_SECRET:
        facilitator_config = create_facilitator_config(
            api_key_id=config.CDP_API_KEY_ID,
            api_key_secret=config.CDP_API_KEY_SECRET,
        )
        facilitator_client = HTTPFacilitatorClient(facilitator_config)
    elif config.ENVIRONMENT == "production":
        raise RuntimeError(
            "CDP_API_KEY_ID/CDP_API_KEY_SECRET are required when ENVIRONMENT=production"
        )
    else:
        from app.dev_facilitator import LocalDevFacilitatorClient

        facilitator_client = LocalDevFacilitatorClient(config.X402_NETWORK)

    # PayAI (facilitator.payai.network) as a SECOND facilitator, CDP listed
    # first: x402ResourceServer resolves one facilitator per (network,
    # scheme) and keeps the first client registered for any combo more than
    # one facilitator supports (x402/server_base.py's
    # _facilitator_clients_map - "if scheme not in map: map[scheme] = client"),
    # so Base/exact traffic keeps resolving to CDP exactly as before. PayAI
    # only engages for a network/scheme CDP does not serve. Free tier, no
    # API key needed to start (docs.payai.network/x402/quickstart,
    # 2026-10-03) - added for PayAI's own Bazaar-style discovery listing
    # (facilitator.payai.network/discovery/resources), not for settlement
    # on routes CDP already covers.
    payai_facilitator_client = HTTPFacilitatorClient(
        FacilitatorConfig(url="https://facilitator.payai.network")
    )
    server = x402ResourceServer([facilitator_client, payai_facilitator_client])
    register_exact_evm_server(server, networks=config.X402_NETWORK)
    from x402.mechanisms.evm.upto.server import UptoEvmScheme as UptoEvmServerScheme

    # POST /v1/chat/completions (app/handlers/llm_gateway.py) is the only
    # route using "upto" (Permit2-based): the buyer authorizes a ceiling,
    # the route settles for real usage x 1.10 afterward via
    # set_settlement_overrides(). Every other route stays on "exact".
    server.register(config.X402_NETWORK, UptoEvmServerScheme())
    server.register_extension(bazaar_resource_server_extension)
    server.on_before_settle(_first_call_free_hook)
    return server


_resource_server_singleton: x402ResourceServer | None = None


def get_resource_server() -> x402ResourceServer:
    """Shared x402ResourceServer instance, built once and reused by both the
    HTTP payment middleware and the MCP tools (app/mcp_server.py) - so there is
    only ever one facilitator client/config in the process, and `.initialize()`
    (which round-trips to the facilitator) only ever runs once regardless of
    whether the first paid call arrives over HTTP or MCP."""
    global _resource_server_singleton
    if _resource_server_singleton is None:
        _resource_server_singleton = build_resource_server()
    return _resource_server_singleton


def display_price(price) -> str:
    """Static, human-readable price for discovery manifests
    (/.well-known/x402, app/mpp_middleware.py's MPP challenge) - a
    DynamicPrice callable (currently only POST /v1/chat/completions'
    compute_ceiling_price, which needs a real request body to resolve) can't
    be shown as-is: passing the bare function through crashed both call
    sites with a 500 (x402/schemas/helpers.py::parse_money got a function
    object where it expected a string - confirmed live, 2026-09-28,
    /.well-known/x402). Substitutes the floor every ceiling is clamped to
    ("$0.001" - app/handlers/llm_gateway.py::MIN_SETTLE_USD) as an
    illustrative starting price; this only affects the informational
    snapshot; the real, request-specific price is still what actually gets
    challenged when a client POSTs for real."""
    if isinstance(price, str):
        return price
    return "$0.001"


def price_label(price) -> str:
    """Human-readable price shown in buyer-facing discovery surfaces
    (/.well-known/x402's accepts[].price, the agent card's skills[].price,
    GET /llms.txt) - NOT fed to the x402 SDK's parse_money (that stays
    display_price() above, unchanged, since resolve_payment_requirements()
    needs a bare "$X" string it can parse into a numeric amount). A static
    route's own price string passes through unchanged. For a DynamicPrice
    callable (POST /v1/chat/completions and the llm/* per-model routes),
    display_price()'s bare "$0.001" floor looks identical to every other
    route's real fixed price - an agent comparing numbers has no way to
    know this one is a floor, not the real cost. This explains how it's
    actually computed instead."""
    if isinstance(price, str):
        return price
    from app.handlers.llm_gateway import MARKUP, MIN_SETTLE_USD

    return f"from ${MIN_SETTLE_USD:.3f}, computed from your max_tokens × model rate × {MARKUP:.2f}"


def resolve_payment_requirements(payment_option: PaymentOption):
    """The one place scheme/network/asset/amount/payTo/maxTimeoutSeconds/extra
    are computed from a PaymentOption - calls the x402 SDK's own
    x402ResourceServer.build_payment_requirements(), the exact function the
    payment middleware itself uses to build a real 402 challenge. Both
    app/discovery.py (/.well-known/x402) and app/mpp_middleware.py (the MPP
    WWW-Authenticate challenge) call this instead of each re-deriving asset/
    amount from price - the well-known listing and the real challenge used to
    disagree (well-known dropped asset/amount entirely) because they didn't."""
    server = get_resource_server()
    if not isinstance(payment_option.price, str):
        from dataclasses import replace

        payment_option = replace(payment_option, price=display_price(payment_option.price))
    try:
        return server.build_payment_requirements(payment_option, extensions=[])[0]
    except RuntimeError:
        # Not yet initialized - happens when /.well-known/x402 is hit before
        # any protected route ever was (the payment middleware initializes
        # lazily on its own first request). Safe to do here too: initialize()
        # is idempotent, this project's own request handling already assumes so.
        server.initialize()
        return server.build_payment_requirements(payment_option, extensions=[])[0]


def build_payment_middleware(app):
    server = get_resource_server()
    # Uncached on purpose (see _build_route_configs_uncached's docstring,
    # incident 2026-10-02): this is the one call where a stale/incomplete
    # route table means unpaid requests get served and marked paid.
    routes = _build_route_configs_uncached()
    return PaymentMiddlewareASGI(app, routes, server)
