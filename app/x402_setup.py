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
KIT_TAGLINE = "Process the files and content an agent gives you. Nothing else."
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
        "Real-time web search - up to 10 results with title, URL, a cleaned page "
        "extract and publish date when available, plus an optional short summary. "
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
        "Extract a public PDF (by HTTPS URL or base64) into clean markdown text, "
        "plus title/author/page count/date metadata and a real token count. "
        "Deterministic text extraction, no model involved. Try GET /pdf/sample. "
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
        'Find MCP servers matching a need, ranked by semantic similarity over a curated snapshot of 10101 MCP servers (registry.modelcontextprotocol.io plus observed registries). Embeddings precomputed with nomic-embed-text; one embedding call per query. Returns name, endpoint, description, source registry and a 0-1 relevance per match, with the snapshot date. Free GET /discover uses the same snapshot (5 default, 10 max via max_results). Paid POST: up to 25 matches and custom min_similarity.'
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
            "description": "Include a cleaned page extract for each result, navigation and boilerplate removed.",
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
                        "description": "Cleaned excerpt of the page's body text, navigation and boilerplate removed.",
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
            "description": "Extracted text, paragraphs preserved, deterministic (no model).",
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
        "Extract a PDF (by URL or base64) into clean markdown with metadata "
        "and a real token count - deterministic, no model."
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
            service_name="AgentIndex Search",
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
            service_name="AgentIndex Research Jobs",
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
            service_name="AgentIndex PDF Extract",
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
            service_name="AgentIndex Web Read",
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
            service_name="AgentIndex Structured Extract",
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
            service_name="AgentIndex Summarize",
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
            service_name="AgentIndex Fact Check",
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
            service_name="AgentIndex Discover",
            tags=KIT_TAGS + ["mcp discovery", "semantic search", "server discovery", "agent registry"],
            extensions=declare_discovery_extension(
                input={"q": "read a PDF and give me markdown"},
                input_schema=DISCOVER_INPUT_SCHEMA,
                body_type="json",
                output=OutputConfig(example=DISCOVER_SAMPLE_OUTPUT, schema=DISCOVER_OUTPUT_SCHEMA),
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
