from cdp.x402 import create_facilitator_config
from x402 import SettleContext, SettleResponse, SkipSettleResult
from x402.mechanisms.evm.exact.register import register_exact_evm_server
from x402.extensions.bazaar import (
    OutputConfig,
    bazaar_resource_server_extension,
    declare_discovery_extension,
)
from x402.http.facilitator_client import HTTPFacilitatorClient
from x402.http.middleware.fastapi import PaymentMiddlewareASGI
from x402.http.types import PaymentOption, RouteConfig
from x402.server import x402ResourceServer

from app import config, db
from app.receipts import extract_payer_from_payment_dict

SEARCH_SAMPLE_OUTPUT = {
    "query": "best ramen restaurants in Shibuya Tokyo",
    "results": [
        {
            "title": "The Best Ramen in Tokyo: 16 Restaurants to Not Miss | Eater",
            "url": "https://www.eater.com/maps/best-ramen-tokyo-japan",
            "date": None,
            "extract": "A curated guide to Tokyo's standout ramen shops, including several in Shibuya...",
        }
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
        "Universal Declaration of Human Rights \nPreamble \nWhereas recognition "
        "of the inherent dignity and of the equal and inalienable \nrights of "
        "all members of the human family is the foundation of freedom, "
        "justice \nand peace in the world, ..."
    ),
    "metadata": {"title": None, "author": "lindner", "pages": 8, "date": "2001-09-10T01:04:58-17:00"},
    "token_count": 2330,
}

WEB_READ_SAMPLE_OUTPUT = {
    "url": "https://www.w3.org/",
    "title": "World Wide Web Consortium (W3C)",
    "markdown": "# World Wide Web Consortium (W3C)\n\nThe W3C mission is to lead the web to its full potential.",
    "token_count": 154,
}

EXTRACT_SAMPLE_OUTPUT = {"data": {"product_name": "Widget Pro", "price": 29.99, "in_stock": True}}

SUMMARIZE_SAMPLE_OUTPUT = {
    "summary": "The article explains how photosynthesis converts light energy into chemical energy in plants.",
    "length": "short",
    "sources": ["https://example.com/photosynthesis"],
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
        "time": "2026-09-19T13:45",
        "temperature_c": 23.8,
        "humidity_pct": 48,
        "wind_speed_kmh": 12.2,
        "weather_code": 1,
        "conditions": "mainly_clear",
    },
    "daily": [
        {
            "date": "2026-09-19",
            "temperature_max_c": 25.1,
            "temperature_min_c": 15.2,
            "precipitation_sum_mm": 0.0,
            "weather_code": 1,
            "conditions": "mainly_clear",
        }
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
    "price_usdc": 0.000001,
    "price_atomic_usdc": "1",
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
                "against 10101 MCP servers (official registry + carnet) by semantic similarity."
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
    "snapshot_rows": 10101,
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
        "Real-time web search and content retrieval - up to 10 results with title, "
        "URL, snippet and publish date, plus clean full-page Markdown for the top "
        "3 results by default and an optional short summary. "
        "Also accepts up to 5 queries in one call, merged and de-duplicated - "
        "one call instead of five. No account, no API key, no quota. Try GET "
        "/search/sample. " + _KIT_MENTION
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
        "Fetch a URL and return its main article as clean markdown - navigation, "
        "ads and boilerplate stripped, links resolved, plus a real token count. "
        "The same extraction /search uses on result pages, exposed standalone "
        "for a URL you already have. Try GET /web-read/sample. " + _KIT_MENTION
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
        "curated snapshot of 10101 MCP servers. Returns name, endpoint, "
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
        "Cheapest possible Base-mainnet x402 conformance call: settle exactly "
        "one atomic unit of USDC ($0.000001) and receive payer identity, request "
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
}

for _name, _desc in ROUTE_DESCRIPTIONS.items():
    assert len(_desc) <= 500, f"description for {_name} is {len(_desc)} chars, must be <=500"

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
        "data": {"type": "object", "description": "Extracted data, strictly conforming to the requested schema."},
        "x402_receipt": {"type": "object", "description": "Billing and provenance receipt for this call."},
    },
    "required": ["data"],
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

# One-sentence outcome summary per route (OpenAPI `summary`, distinct from the
# longer `description`) and imperative-phrased agent intents (`x-use-cases`,
# not a standard field but harmless if unread, and cheap extra signal if
# AgentCash's indexer scans the whole operation body for embedding text).
ROUTE_SUMMARIES = {
    "discover": (
        "Match a need in plain language against a curated snapshot of 10101 "
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
        "Prove an x402 mainnet client works with a one-atomic-USDC settlement."
    ),
    "agent-health": (
        "Verify that an agent is reachable, discoverable and payment-ready now."
    ),
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
        "obtain payer identity and a receipt from a one-atomic-USDC call",
        "exercise the complete 402 sign retry settle flow in CI",
    ],
    "agent-health": [
        "verify an agent is operational before routing a paid job to it",
        "audit x402 price, network and recipient before signing a payment",
        "check OpenAPI, A2A agent card and MCP discovery in one call",
        "score a third-party agent and receive explicit remediation issues",
    ],
}


def _payment_option(price: str) -> PaymentOption:
    return PaymentOption(
        scheme="exact",
        pay_to=config.X402_PAY_TO,
        price=price,
        network=config.X402_NETWORK,
    )


def build_route_configs() -> dict[str, RouteConfig]:
    # The 3 hand-built core routes, plus anything the usine (Prospecteur/
    # Ouvrier/Crieur - see usine/) has added to app/generated/routes_registry.yaml.
    # Merged here so x402 challenges, the well-known, and Bazaar/MCP discovery
    # never need a second source of truth for generated routes.
    from app.generated.dynamic_routes import build_dynamic_route_configs

    return {
        **_core_route_configs(),
        **build_dynamic_route_configs(),
    }


def _core_route_configs() -> dict[str, RouteConfig]:
    return {
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
            tags=KIT_TAGS + ["web search", "live search", "SERP", "multi-query"],
            extensions=declare_discovery_extension(
                input={"query": "best ramen restaurants in Shibuya Tokyo"},
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
            tags=KIT_TAGS + ["pdf", "document parsing", "metadata"],
            extensions=declare_discovery_extension(
                input={"url": "https://example.com/report.pdf"},
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
            tags=KIT_TAGS + ["article extraction", "readability", "page to markdown"],
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
            tags=KIT_TAGS + ["structured data", "json schema", "web scraping"],
            extensions=declare_discovery_extension(
                input={
                    "url": "https://example.com/product/widget-pro",
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
            tags=KIT_TAGS + ["summarization", "tl;dr"],
            extensions=declare_discovery_extension(
                input={"url": "https://example.com/photosynthesis", "length": "short"},
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
            tags=KIT_TAGS + ["fact checking", "claim verification", "verification"],
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
            tags=KIT_TAGS + ["mcp discovery", "semantic search", "server discovery", "agent registry"],
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
            tags=["crypto", "price", "bitcoin", "ethereum", "spot", "market data"],
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
            tags=["crypto", "price", "bitcoin", "ethereum", "spot", "market data"],
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
            tags=["wallet", "usdc", "balance", "preflight", "base", "can pay"],
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
            tags=["wallet", "usdc", "balance", "preflight", "base", "can pay"],
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
            tags=["wallet", "balance", "USDC", "ERC-20", "EVM", "onchain", "RPC"],
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
            tags=["wallet", "balance", "USDC", "ERC-20", "EVM", "onchain", "RPC"],
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
            tags=["gas", "fee", "EIP-1559", "EVM", "onchain", "RPC"],
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
            tags=["gas", "fee", "EIP-1559", "EVM", "onchain", "RPC"],
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
            tags=[
                "wallet intelligence", "multi-chain", "USDC", "gas",
                "payment preflight", "EVM", "RPC bundle",
            ],
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
            tags=[
                "wallet intelligence", "multi-chain", "USDC", "gas",
                "payment preflight", "EVM", "RPC bundle",
            ],
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
            accepts=_payment_option(config.PRICE_X402_ECHO),
            resource=f"{config.BASE_URL}/x402-echo",
            description=ROUTE_DESCRIPTIONS["x402-echo"],
            mime_type="application/json",
            service_name="x402-payment-test",
            tags=[
                "x402 test", "mainnet conformance", "payment signature",
                "USDC atomic", "facilitator test", "echo",
            ],
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
            accepts=_payment_option(config.PRICE_X402_ECHO),
            resource=f"{config.BASE_URL}/x402-echo",
            description=ROUTE_DESCRIPTIONS["x402-echo"],
            mime_type="application/json",
            service_name="x402-payment-test",
            tags=[
                "x402 test", "mainnet conformance", "payment signature",
                "USDC atomic", "facilitator test", "echo",
            ],
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
            tags=["agent registry", "verification badge", "discovery", "trust", "x402"],
            extensions=declare_discovery_extension(
                input={"url": "https://agent.example", "name": "Example Agent"},
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
                        "url": "https://agent.example",
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
            tags=[
                "agent health", "uptime", "MCP", "A2A", "x402 audit",
                "latency", "operational status", "monitoring",
            ],
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
            tags=[
                "agent health", "uptime", "MCP", "A2A", "x402 audit",
                "latency", "operational status", "monitoring",
            ],
            extensions=declare_discovery_extension(
                input={"url": "https://x402.agentindex.world/search", "method": "POST"},
                input_schema=AGENT_HEALTH_INPUT_SCHEMA,
                output=OutputConfig(
                    example=AGENT_HEALTH_SAMPLE_OUTPUT,
                    schema=AGENT_HEALTH_OUTPUT_SCHEMA,
                ),
            ),
        ),
    }


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

    server = x402ResourceServer(facilitator_client)
    register_exact_evm_server(server, networks=config.X402_NETWORK)
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
    routes = build_route_configs()
    return PaymentMiddlewareASGI(app, routes, server)
