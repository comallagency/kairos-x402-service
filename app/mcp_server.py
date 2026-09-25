"""MCP server exposing the same 3 paid services (search, translate, jobs) as MCP
tools, mounted into the same FastAPI app as the HTTP routes (see app/main.py).

Why this does NOT use x402.mcp.server_async.wrap_fastmcp_tool / create_payment_wrapper:
those helpers are broken against x402==2.22.0 as shipped - server_async.py's
wrap_fastmcp_tool does `from .server import _extract_meta_from_fastmcp_context,
_mcp_tool_result_to_call_tool_result`, but x402/mcp/server.py (the sync/decorator
flavored module) defines neither name. Confirmed by running the wrapper against a
real FastMCP instance (see scripts/_mcp_probe.py): ImportError, not a fastmcp
version mismatch. See BRIEF-CORRECTIONS.md for the full writeup.

Separately, x402/mcp/server.py's OWN create_payment_wrapper (the one that does
work in isolation) imports `from mcp.server.fastmcp import Context` - the
OFFICIAL mcp SDK's bundled FastMCP, a different class than the standalone
`fastmcp` package's `fastmcp.server.context.Context` this project actually uses
(fastmcp==4.0.3). Its signature-injection trick (`issubclass(annotation, Context)`)
would silently never fire against fastmcp 4.x tools, so payment metadata would
never be found even though nothing raises.

Given both paths are dead ends, this module implements the same verify -> run ->
settle flow directly against x402ResourceServer (build_payment_requirements,
verify_payment, settle_payment, create_payment_required_response,
find_matching_requirements - all confirmed by introspection, matching
BRIEF-CORRECTIONS.md's warning that the SDK's docstrings reference methods that
don't exist). This mirrors exactly what x402.http.middleware.fastapi's
PaymentMiddlewareASGI does for the HTTP routes (verify -> call handler -> settle
only on success - see PaymentMiddlewareASGI.dispatch), so behavior (and price,
since it reuses the same RouteConfig/PaymentOption objects) can never drift
between the HTTP and MCP surfaces.

Payment travels as MCP request `_meta["x402/payment"]`, lifted by fastmcp into
`ctx.request_context.meta` (fastmcp.server.dependencies.FastMCPRequestContext) -
the same key (`x402.mcp.types.MCP_PAYMENT_META_KEY`) and extraction helper
(`x402.mcp.utils.extract_payment_from_meta`) the SDK's own MCP client helpers use,
so any x402-aware MCP client (including x402.mcp.client_async.x402MCPClient, or a
plain fastmcp.Client passing `meta=...`) can pay these tools without
project-specific client code.
"""

import asyncio
import json
import logging

from fastmcp import Context, FastMCP
from fastmcp.tools.base import ToolResult
from fastmcp.tools.function_tool import FunctionTool

from x402.extensions.bazaar import DeclareMcpDiscoveryConfig, OutputConfig, declare_mcp_discovery_extension
from x402.mcp.types import MCP_PAYMENT_RESPONSE_META_KEY
from x402.mcp.utils import extract_payment_from_meta
from x402.schemas import ResourceInfo

from app import config, db
from app.generated.proxy_handler import call_upstream, price_float
from app.generated.registry import RouteSpec, live_routes
from app.handlers.detect_language import _identifier as _language_identifier
from app.handlers.discover import DESCRIPTION as DISCOVER_DESCRIPTION
from app.handlers.discover import MAX_RESULTS_CAP as DISCOVER_MAX_RESULTS
from app.handlers.discover import _paid_upgrade_hint
from app.handlers.discover_paid import _run_discover as _run_discover_snapshot
from app.handlers.discover_paid import MIN_SIMILARITY
from app.handlers.discover_paid import _run_discover as _run_discover_semantic
from app.upstream.ollama import OllamaError
from app.upstream.websearch import SearchError
from app.handlers.extract import ExtractError, _get_content as _get_extract_content, _run_extract
from app.handlers.fact_check import FactCheckError, _check_claim
from app.handlers.jobs import JOB_POLL_SLOT_SECONDS
from app.handlers.pdf import PdfError, _get_pdf_bytes, parse_pdf
from app.handlers.search import (
    DEFAULT_CONTENT_CHARS,
    MAX_BATCH_QUERIES,
    _clamp_max_results,
    _clamp_content_chars,
    _enrich_results,
    _run_batch_search,
    _shape_results,
    _summarize,
)
from app.handlers.summarize import (
    SummarizeError,
    _get_content_and_sources as _get_summarize_content_and_sources,
    _summarize_content,
)
from app.handlers.translate import MAX_BATCH_SEGMENTS, _translate, _translate_batch
from app.handlers.web_read import WebReadError, _read_url
from app.receipts import (
    Timer,
    effective_price,
    extract_payer_from_payment_dict,
    make_receipt,
    neutral_model_id,
    price_float,
)
from app.upstream.openrouter import OpenRouterError
from app.upstream.tokencount import count_tokens
from app.upstream.websearch import run_web_search
from app.x402_setup import (
    CRYPTO_INPUT_SCHEMA,
    CRYPTO_SAMPLE_OUTPUT,
    EXTRACT_INPUT_SCHEMA,
    EXTRACT_SAMPLE_OUTPUT,
    FACT_CHECK_INPUT_SCHEMA,
    FACT_CHECK_SAMPLE_OUTPUT,
    JOBS_INPUT_SCHEMA,
    JOBS_SAMPLE_OUTPUT,
    KIT_TAGLINE,
    NEWS_INPUT_SCHEMA,
    NEWS_SAMPLE_OUTPUT,
    PDF_INPUT_SCHEMA,
    PDF_SAMPLE_OUTPUT,
    ROUTE_DESCRIPTIONS,
    SEARCH_INPUT_SCHEMA,
    SEARCH_SAMPLE_OUTPUT,
    SUMMARIZE_INPUT_SCHEMA,
    SUMMARIZE_SAMPLE_OUTPUT,
    TRANSLATE_INPUT_SCHEMA,
    TRANSLATE_SAMPLE_OUTPUT,
    WEATHER_INPUT_SCHEMA,
    WEATHER_SAMPLE_OUTPUT,
    WEB_READ_INPUT_SCHEMA,
    WEB_READ_SAMPLE_OUTPUT,
    CAN_PAY_INPUT_SCHEMA,
    CAN_PAY_SAMPLE_OUTPUT,
    PROBE_INPUT_SCHEMA,
    PROBE_SAMPLE_OUTPUT,
    WALLET_BALANCE_INPUT_SCHEMA,
    WALLET_BALANCE_SAMPLE_OUTPUT,
    GAS_PRICE_INPUT_SCHEMA,
    GAS_PRICE_SAMPLE_OUTPUT,
    WALLET_INTELLIGENCE_INPUT_SCHEMA,
    WALLET_INTELLIGENCE_SAMPLE_OUTPUT,
    X402_ECHO_INPUT_SCHEMA,
    X402_ECHO_SAMPLE_OUTPUT,
    AGENT_HEALTH_INPUT_SCHEMA,
    AGENT_HEALTH_SAMPLE_OUTPUT,
    DISCOVER_INPUT_SCHEMA,
    DISCOVER_SAMPLE_OUTPUT,
    build_route_configs,
    get_resource_server,
)

logger = logging.getLogger("x402.mcp_server")

mcp = FastMCP(
    "AgentIndex x402",
    instructions=(
        f"{KIT_TAGLINE} Paid per call in USDC on Base (x402) - no "
        "account, no API key. FIRST CALL (feeds CDP Bazaar): can_pay "
        "(Base USDC balance check) or probe (detect x402 paywall on a URL) "
        "at $0.001. Then weather, crypto, news, discover, search, "
        "read_pdf, read_web_page, extract_structured, summarize, "
        "fact_check, translate, jobs."
    ),
)

_route_configs = build_route_configs()
_init_lock = asyncio.Lock()


class ServiceError(Exception):
    """Raised by a tool's business-logic step (not a payment problem) - handled by
    _paid_tool_call by returning a shaped error result WITHOUT settling the
    payment, matching PaymentMiddlewareASGI's "don't settle on error responses"."""

    def __init__(self, reason: str, detail: str | None = None):
        self.reason = reason
        self.detail = detail
        super().__init__(reason)


async def _ensure_initialized():
    server = get_resource_server()
    if not getattr(server, "_initialized", False):
        async with _init_lock:
            if not getattr(server, "_initialized", False):
                server.initialize()
    return server


def _resource_info(tool_name: str, route_key: str) -> ResourceInfo:
    rc = _route_configs[route_key]
    return ResourceInfo(
        url=f"mcp://tool/{tool_name}",
        description=rc.description,
        mime_type=rc.mime_type,
        service_name=rc.service_name,
        tags=rc.tags,
    )


async def _payment_required_result(server, accepts, resource_info, extensions, error_message) -> ToolResult:
    payment_required = await server.create_payment_required_response(
        accepts, resource_info, error_message, extensions
    )
    body = payment_required.model_dump(by_alias=True, exclude_none=True)
    return ToolResult(structured_content=body, is_error=True)


def _settlement_failed_result(accepts, resource_info, extensions, settle_result) -> ToolResult:
    body = {
        "x402Version": 2,
        "accepts": [req.model_dump(by_alias=True, exclude_none=True) for req in accepts],
        "error": f"Payment settlement failed: {settle_result.error_reason or 'unknown'}",
        "resource": resource_info.model_dump(by_alias=True, exclude_none=True),
    }
    if extensions:
        body["extensions"] = extensions
    body[MCP_PAYMENT_RESPONSE_META_KEY] = {
        "success": False,
        "errorReason": settle_result.error_reason,
        "transaction": "",
        "network": accepts[0].network,
    }
    return ToolResult(structured_content=body, is_error=True)


async def _paid_tool_call(*, tool_name: str, route_key: str, ctx: Context | None, args: dict, extensions: dict, run_and_log) -> ToolResult:
    """Shared verify -> run business logic -> settle flow for all 3 tools."""
    server = await _ensure_initialized()
    route_config = _route_configs[route_key]
    accepts = server.build_payment_requirements(route_config.accepts)
    resource_info = _resource_info(tool_name, route_key)

    meta: dict = {}
    if ctx is not None:
        request_context = ctx.request_context
        if request_context is not None and request_context.meta:
            meta = request_context.meta

    payment_payload = extract_payment_from_meta({"_meta": meta})
    if payment_payload is None:
        return await _payment_required_result(
            server, accepts, resource_info, extensions, "Payment required to access this tool"
        )

    payment_requirements = server.find_matching_requirements(accepts, payment_payload)
    if payment_requirements is None:
        return await _payment_required_result(
            server, accepts, resource_info, extensions, "No matching payment requirements found"
        )

    verify_result = await server.verify_payment(payment_payload, payment_requirements)
    if not verify_result.is_valid:
        return await _payment_required_result(
            server,
            accepts,
            resource_info,
            extensions,
            f"Payment verification failed: {verify_result.invalid_reason}",
        )

    payer = extract_payer_from_payment_dict(
        payment_payload.model_dump(by_alias=True, exclude_none=True)
    )

    try:
        result_payload = await run_and_log(args, payer)
    except ServiceError as exc:
        body = {"error": {"reason": exc.reason}}
        if exc.detail:
            body["error"]["detail"] = exc.detail
        return ToolResult(structured_content=body, is_error=True)
    except Exception:
        logger.exception("mcp tool %s: unexpected handler error", tool_name)
        return ToolResult(content=[{"type": "text", "text": "Internal Server Error"}], is_error=True)

    settle_result = await server.settle_payment(payment_payload, payment_requirements)
    if not settle_result.success:
        return _settlement_failed_result(accepts, resource_info, extensions, settle_result)

    settle_meta = settle_result.model_dump(by_alias=True, exclude_none=True)
    return ToolResult(
        structured_content=result_payload,
        meta={MCP_PAYMENT_RESPONSE_META_KEY: settle_meta},
    )


# --- search ---------------------------------------------------------------

_SEARCH_EXTENSIONS = declare_mcp_discovery_extension(
    DeclareMcpDiscoveryConfig(
        tool_name="search",
        description=ROUTE_DESCRIPTIONS["search"],
        input_schema=SEARCH_INPUT_SCHEMA,
        example={"query": "best ramen restaurants in Shibuya Tokyo"},
        output=OutputConfig(example=SEARCH_SAMPLE_OUTPUT),
    )
)


async def _run_search(args: dict, payer: str | None) -> dict:
    body_excerpt = json.dumps(args)
    query = args.get("query")
    if not query:
        db.log_request(
            route="search", method="MCP", status="error", payer=payer,
            user_agent="mcp", body_excerpt=body_excerpt, error_reason="missing_query",
        )
        raise ServiceError("missing_query")

    is_batch = isinstance(query, list)
    if is_batch and not (
        1 <= len(query) <= MAX_BATCH_QUERIES and all(isinstance(q, str) and q for q in query)
    ):
        db.log_request(
            route="search", method="MCP", status="error", payer=payer,
            user_agent="mcp", body_excerpt=body_excerpt, error_reason="invalid_batch_query",
        )
        raise ServiceError(
            "invalid_batch_query", detail=f"query array must have 1-{MAX_BATCH_QUERIES} non-empty strings"
        )

    max_results = _clamp_max_results(args.get("max_results", 5))
    extract = bool(args.get("extract", True))
    include_content = bool(args.get("include_content", True))
    content_results = args.get("content_results", 3)
    content_chars = _clamp_content_chars(args.get("content_chars", DEFAULT_CONTENT_CHARS))
    summarize = bool(args.get("summarize", False))

    try:
        with Timer() as t:
            if is_batch:
                results, model_served = await _run_batch_search(query, max_results)
                summary_label = "; ".join(query)
            else:
                results, model_served = await run_web_search(query, max_results)
                summary_label = query
            results = await _enrich_results(
                results, include_content, content_results, content_chars
            )
            summary = await _summarize(summary_label, results) if summarize else None
    except (OpenRouterError, SearchError) as exc:
        db.log_request(
            route="search", method="MCP", status="error", payer=payer,
            user_agent="mcp", body_excerpt=body_excerpt, error_reason=str(exc)[:200],
        )
        raise ServiceError("upstream_error", detail=str(exc)[:200]) from exc

    price = effective_price(payer, price_float(config.PRICE_SEARCH))
    db.log_request(
        route="search", method="MCP", status="paid", latency_ms=t.elapsed_ms, amount_usdc=price, payer=payer,
        user_agent="mcp", body_excerpt=body_excerpt,
    )
    receipt = make_receipt(
        neutral_model_id(model_served), "web_search", t.elapsed_ms, price,
        searches_run=(len(query) if is_batch else 1), sources_read=len(results),
    )
    response = {"query": query, "results": _shape_results(results, extract), "x402_receipt": receipt}
    if summarize:
        response["summary"] = summary
    return response


# Disabled 2026-09-07 along with "POST /search" in app/x402_setup.py -
# depends on OpenRouter's paid "web" plugin, which the account's negative
# credit balance turns into "charge the buyer, return a 502" (see the
# KIT_TAGLINE comment in app/x402_setup.py). Function and helpers kept
# intact; uncomment this decorator to re-register the tool once a real
# balance exists.
@mcp.tool(name="search", description=ROUTE_DESCRIPTIONS["search"])
async def search_tool(
    query: str | list[str],
    max_results: int = 5,
    extract: bool = True,
    include_content: bool = True,
    content_results: int = 3,
    content_chars: int = DEFAULT_CONTENT_CHARS,
    summarize: bool = False,
    ctx: Context = None,
) -> ToolResult:
    args = {
        "query": query,
        "max_results": max_results,
        "extract": extract,
        "include_content": include_content,
        "content_results": content_results,
        "content_chars": content_chars,
        "summarize": summarize,
    }
    return await _paid_tool_call(
        tool_name="search",
        route_key="POST /search",
        ctx=ctx,
        args=args,
        extensions=_SEARCH_EXTENSIONS,
        run_and_log=_run_search,
    )


# --- translate --------------------------------------------------------------

_TRANSLATE_EXTENSIONS = declare_mcp_discovery_extension(
    DeclareMcpDiscoveryConfig(
        tool_name="translate",
        description=ROUTE_DESCRIPTIONS["translate"],
        input_schema=TRANSLATE_INPUT_SCHEMA,
        example={"text": "Bonjour le monde", "target_lang": "en"},
        output=OutputConfig(example=TRANSLATE_SAMPLE_OUTPUT),
    )
)


async def _run_translate(args: dict, payer: str | None) -> dict:
    body_excerpt = json.dumps(args)
    text = args.get("text")
    target_lang = args.get("target_lang")
    if not text or not target_lang:
        db.log_request(
            route="translate", method="MCP", status="error", payer=payer,
            user_agent="mcp", body_excerpt=body_excerpt, error_reason="missing_fields",
        )
        raise ServiceError("missing_fields")

    is_batch = isinstance(text, list)
    if is_batch and not (1 <= len(text) <= MAX_BATCH_SEGMENTS and all(isinstance(s, str) for s in text)):
        db.log_request(
            route="translate", method="MCP", status="error", payer=payer,
            user_agent="mcp", body_excerpt=body_excerpt, error_reason="invalid_batch_text",
        )
        raise ServiceError(
            "invalid_batch_text", detail=f"text array must have 1-{MAX_BATCH_SEGMENTS} string segments"
        )

    source_lang = args.get("source_lang")
    preserve_format = bool(args.get("preserve_format", True))

    try:
        with Timer() as t:
            if is_batch:
                translations, detected_source_lang, model_served, fallback_used = await _translate_batch(
                    text, target_lang, source_lang, preserve_format
                )
            else:
                parsed, model_served, fallback_used = await _translate(
                    text, target_lang, source_lang, preserve_format
                )
    except OpenRouterError as exc:
        db.log_request(
            route="translate", method="MCP", status="error", payer=payer,
            user_agent="mcp", body_excerpt=body_excerpt, error_reason=str(exc)[:200],
        )
        raise ServiceError("upstream_error", detail=str(exc)[:200]) from exc

    price = effective_price(payer, price_float(config.PRICE_TRANSLATE))
    db.log_request(
        route="translate", method="MCP", status="paid", latency_ms=t.elapsed_ms, amount_usdc=price, payer=payer,
        user_agent="mcp", body_excerpt=body_excerpt,
    )

    if is_batch:
        receipt = make_receipt(
            neutral_model_id(model_served), "llm", t.elapsed_ms, price,
            fallback_used=fallback_used, segments_processed=len(text),
        )
        return {
            "text": text,
            "target_lang": target_lang,
            "detected_source_lang": detected_source_lang,
            "translated_text": translations,
            "x402_receipt": receipt,
        }

    receipt = make_receipt(neutral_model_id(model_served), "llm", t.elapsed_ms, price, fallback_used=fallback_used)
    return {
        "text": text,
        "target_lang": target_lang,
        "detected_source_lang": parsed.get("detected_source_lang"),
        "translated_text": parsed.get("translated_text"),
        "x402_receipt": receipt,
    }


@mcp.tool(name="translate", description=ROUTE_DESCRIPTIONS["translate"])
async def translate_tool(
    text: str | list[str],
    target_lang: str,
    source_lang: str | None = None,
    preserve_format: bool = True,
    ctx: Context = None,
) -> ToolResult:
    args = {
        "text": text,
        "target_lang": target_lang,
        "source_lang": source_lang,
        "preserve_format": preserve_format,
    }
    return await _paid_tool_call(
        tool_name="translate",
        route_key="POST /translate",
        ctx=ctx,
        args=args,
        extensions=_TRANSLATE_EXTENSIONS,
        run_and_log=_run_translate,
    )


# --- jobs ---------------------------------------------------------------

_JOBS_EXTENSIONS = declare_mcp_discovery_extension(
    DeclareMcpDiscoveryConfig(
        tool_name="jobs",
        description=ROUTE_DESCRIPTIONS["jobs"],
        input_schema=JOBS_INPUT_SCHEMA,
        example={"subject": "Example Corp"},
        output=OutputConfig(example={"job_id": JOBS_SAMPLE_OUTPUT["job_id"], "eta_seconds": 60}),
    )
)


async def _run_jobs(args: dict, payer: str | None) -> dict:
    body_excerpt = json.dumps(args)
    subject = args.get("subject")
    if not subject:
        db.log_request(
            route="jobs", method="MCP", status="error", payer=payer,
            user_agent="mcp", body_excerpt=body_excerpt, error_reason="missing_subject",
        )
        raise ServiceError("missing_subject")

    price = effective_price(payer, price_float(config.PRICE_JOB))
    job_id = db.create_job({"subject": subject}, amount_usdc=price, payer=payer)
    position = db.queue_position(job_id)
    eta_seconds = (position + 1) * JOB_POLL_SLOT_SECONDS

    db.log_request(
        route="jobs", method="MCP", status="paid", amount_usdc=price, payer=payer,
        user_agent="mcp", body_excerpt=body_excerpt,
    )
    return {"job_id": job_id, "eta_seconds": eta_seconds}


@mcp.tool(name="jobs", description=ROUTE_DESCRIPTIONS["jobs"])
async def jobs_tool(subject: str, ctx: Context = None) -> ToolResult:
    args = {"subject": subject}
    return await _paid_tool_call(
        tool_name="jobs",
        route_key="POST /jobs",
        ctx=ctx,
        args=args,
        extensions=_JOBS_EXTENSIONS,
        run_and_log=_run_jobs,
    )


# --- read_pdf (POST /pdf) --------------------------------------------------

_PDF_EXTENSIONS = declare_mcp_discovery_extension(
    DeclareMcpDiscoveryConfig(
        tool_name="read_pdf",
        description=ROUTE_DESCRIPTIONS["pdf"],
        input_schema=PDF_INPUT_SCHEMA,
        example={"url": "https://example.com/report.pdf"},
        output=OutputConfig(example=PDF_SAMPLE_OUTPUT),
    )
)


async def _run_read_pdf(args: dict, payer: str | None) -> dict:
    body_excerpt = json.dumps(args)
    try:
        with Timer() as t:
            data = await _get_pdf_bytes(args)
            parsed = parse_pdf(data)
    except PdfError as exc:
        db.log_request(
            route="pdf", method="MCP", status="error", payer=payer,
            user_agent="mcp", body_excerpt=body_excerpt, error_reason=exc.reason,
        )
        raise ServiceError(exc.reason) from exc

    token_count = count_tokens(parsed["markdown"])
    price = effective_price(payer, price_float(config.PRICE_PDF))
    db.log_request(
        route="pdf", method="MCP", status="paid", latency_ms=t.elapsed_ms, amount_usdc=price, payer=payer,
        user_agent="mcp", body_excerpt=body_excerpt,
    )
    receipt = make_receipt(None, "pdf_parse", t.elapsed_ms, price)
    return {**parsed, "token_count": token_count, "x402_receipt": receipt}


@mcp.tool(name="read_pdf", description=ROUTE_DESCRIPTIONS["pdf"])
async def read_pdf_tool(url: str | None = None, pdf_base64: str | None = None, ctx: Context = None) -> ToolResult:
    args = {"url": url, "pdf_base64": pdf_base64}
    return await _paid_tool_call(
        tool_name="read_pdf",
        route_key="POST /pdf",
        ctx=ctx,
        args=args,
        extensions=_PDF_EXTENSIONS,
        run_and_log=_run_read_pdf,
    )


# --- read_web_page (POST /web-read) ----------------------------------------

_WEB_READ_EXTENSIONS = declare_mcp_discovery_extension(
    DeclareMcpDiscoveryConfig(
        tool_name="read_web_page",
        description=ROUTE_DESCRIPTIONS["web-read"],
        input_schema=WEB_READ_INPUT_SCHEMA,
        example={"url": "https://www.w3.org/"},
        output=OutputConfig(example=WEB_READ_SAMPLE_OUTPUT),
    )
)


async def _run_read_web_page(args: dict, payer: str | None) -> dict:
    body_excerpt = json.dumps(args)
    url = args.get("url")
    if not url or not isinstance(url, str):
        db.log_request(
            route="web-read", method="MCP", status="error", payer=payer,
            user_agent="mcp", body_excerpt=body_excerpt, error_reason="missing_url",
        )
        raise ServiceError("missing_url")
    try:
        with Timer() as t:
            result = await _read_url(url)
    except WebReadError as exc:
        db.log_request(
            route="web-read", method="MCP", status="error", payer=payer,
            user_agent="mcp", body_excerpt=body_excerpt, error_reason=exc.reason,
        )
        raise ServiceError(exc.reason) from exc

    token_count = count_tokens(result["markdown"])
    price = effective_price(payer, price_float(config.PRICE_WEB_READ))
    db.log_request(
        route="web-read", method="MCP", status="paid", latency_ms=t.elapsed_ms, amount_usdc=price, payer=payer,
        user_agent="mcp", body_excerpt=body_excerpt,
    )
    receipt = make_receipt(None, "web_fetch", t.elapsed_ms, price, sources_read=1)
    return {**result, "token_count": token_count, "x402_receipt": receipt}


@mcp.tool(name="read_web_page", description=ROUTE_DESCRIPTIONS["web-read"])
async def read_web_page_tool(url: str, ctx: Context = None) -> ToolResult:
    return await _paid_tool_call(
        tool_name="read_web_page",
        route_key="POST /web-read",
        ctx=ctx,
        args={"url": url},
        extensions=_WEB_READ_EXTENSIONS,
        run_and_log=_run_read_web_page,
    )


# --- extract_structured (POST /extract) ------------------------------------

_EXTRACT_EXTENSIONS = declare_mcp_discovery_extension(
    DeclareMcpDiscoveryConfig(
        tool_name="extract_structured",
        description=ROUTE_DESCRIPTIONS["extract"],
        input_schema=EXTRACT_INPUT_SCHEMA,
        example={
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
        output=OutputConfig(example=EXTRACT_SAMPLE_OUTPUT),
    )
)


async def _run_extract_structured(args: dict, payer: str | None) -> dict:
    body_excerpt = json.dumps(args)
    schema = args.get("schema")
    if not schema or not isinstance(schema, dict):
        db.log_request(
            route="extract", method="MCP", status="error", payer=payer,
            user_agent="mcp", body_excerpt=body_excerpt, error_reason="missing_schema",
        )
        raise ServiceError("missing_schema")
    try:
        with Timer() as t:
            content = await _get_extract_content(args)
            data, model_served = await _run_extract(content, schema)
    except ExtractError as exc:
        db.log_request(
            route="extract", method="MCP", status="error", payer=payer,
            user_agent="mcp", body_excerpt=body_excerpt, error_reason=exc.reason,
        )
        raise ServiceError(exc.reason, detail=exc.detail) from exc

    price = effective_price(payer, price_float(config.PRICE_EXTRACT))
    db.log_request(
        route="extract", method="MCP", status="paid", latency_ms=t.elapsed_ms, amount_usdc=price, payer=payer,
        user_agent="mcp", body_excerpt=body_excerpt,
    )
    receipt = make_receipt(neutral_model_id(model_served), "llm", t.elapsed_ms, price)
    return {"data": data, "x402_receipt": receipt}


@mcp.tool(name="extract_structured", description=ROUTE_DESCRIPTIONS["extract"])
async def extract_structured_tool(
    schema: dict, url: str | None = None, text: str | None = None, ctx: Context = None
) -> ToolResult:
    args = {"schema": schema, "url": url, "text": text}
    return await _paid_tool_call(
        tool_name="extract_structured",
        route_key="POST /extract",
        ctx=ctx,
        args=args,
        extensions=_EXTRACT_EXTENSIONS,
        run_and_log=_run_extract_structured,
    )


# --- summarize (POST /summarize) --------------------------------------------

_SUMMARIZE_EXTENSIONS = declare_mcp_discovery_extension(
    DeclareMcpDiscoveryConfig(
        tool_name="summarize",
        description=ROUTE_DESCRIPTIONS["summarize"],
        input_schema=SUMMARIZE_INPUT_SCHEMA,
        example={"url": "https://example.com/photosynthesis", "length": "short"},
        output=OutputConfig(example=SUMMARIZE_SAMPLE_OUTPUT),
    )
)


async def _run_summarize(args: dict, payer: str | None) -> dict:
    body_excerpt = json.dumps(args)
    length = args.get("length") or "medium"
    try:
        with Timer() as t:
            content, sources = await _get_summarize_content_and_sources(args)
            summary, model_served = await _summarize_content(content, length)
    except SummarizeError as exc:
        db.log_request(
            route="summarize", method="MCP", status="error", payer=payer,
            user_agent="mcp", body_excerpt=body_excerpt, error_reason=exc.reason,
        )
        raise ServiceError(exc.reason, detail=exc.detail) from exc

    price = effective_price(payer, price_float(config.PRICE_SUMMARIZE))
    db.log_request(
        route="summarize", method="MCP", status="paid", latency_ms=t.elapsed_ms, amount_usdc=price, payer=payer,
        user_agent="mcp", body_excerpt=body_excerpt,
    )
    receipt = make_receipt(neutral_model_id(model_served), "llm", t.elapsed_ms, price)
    return {"summary": summary, "length": length, "sources": sources, "x402_receipt": receipt}


@mcp.tool(name="summarize", description=ROUTE_DESCRIPTIONS["summarize"])
async def summarize_tool(
    url: str | None = None, text: str | None = None, html: str | None = None,
    length: str = "medium", ctx: Context = None,
) -> ToolResult:
    args = {"url": url, "text": text, "html": html, "length": length}
    return await _paid_tool_call(
        tool_name="summarize",
        route_key="POST /summarize",
        ctx=ctx,
        args=args,
        extensions=_SUMMARIZE_EXTENSIONS,
        run_and_log=_run_summarize,
    )


# --- fact_check (POST /fact-check) ------------------------------------------

_FACT_CHECK_EXTENSIONS = declare_mcp_discovery_extension(
    DeclareMcpDiscoveryConfig(
        tool_name="fact_check",
        description=ROUTE_DESCRIPTIONS["fact-check"],
        input_schema=FACT_CHECK_INPUT_SCHEMA,
        example={"claim": "The Eiffel Tower is taller than the Statue of Liberty."},
        output=OutputConfig(example=FACT_CHECK_SAMPLE_OUTPUT),
    )
)


async def _run_fact_check(args: dict, payer: str | None) -> dict:
    body_excerpt = json.dumps(args)
    claim = args.get("claim")
    if not claim or not isinstance(claim, str):
        db.log_request(
            route="fact-check", method="MCP", status="error", payer=payer,
            user_agent="mcp", body_excerpt=body_excerpt, error_reason="missing_claim",
        )
        raise ServiceError("missing_claim")
    try:
        with Timer() as t:
            result, model_served, sources_read, queries_run = await _check_claim(claim)
    except FactCheckError as exc:
        db.log_request(
            route="fact-check", method="MCP", status="error", payer=payer,
            user_agent="mcp", body_excerpt=body_excerpt, error_reason=exc.reason,
        )
        raise ServiceError(exc.reason, detail=exc.detail) from exc

    price = effective_price(payer, price_float(config.PRICE_FACT_CHECK))
    db.log_request(
        route="fact-check", method="MCP", status="paid", latency_ms=t.elapsed_ms, amount_usdc=price, payer=payer,
        user_agent="mcp", body_excerpt=body_excerpt,
    )
    receipt = make_receipt(
        neutral_model_id(model_served), "web_search+llm", t.elapsed_ms, price,
        searches_run=queries_run, sources_read=sources_read,
    )
    return {"claim": claim, **result, "x402_receipt": receipt}


# Disabled 2026-09-07 along with "POST /fact-check" in app/x402_setup.py -
# _check_claim() searches the web internally via the same paid plugin as
# /search. See the comment above search_tool.
@mcp.tool(name="fact_check", description=ROUTE_DESCRIPTIONS["fact-check"])
async def fact_check_tool(claim: str, ctx: Context = None) -> ToolResult:
    return await _paid_tool_call(
        tool_name="fact_check",
        route_key="POST /fact-check",
        ctx=ctx,
        args={"claim": claim},
        extensions=_FACT_CHECK_EXTENSIONS,
        run_and_log=_run_fact_check,
    )


# --- weather / crypto / news ($0.001 commodity) -----------------------------

from app.handlers.crypto import CryptoError, _lookup as _crypto_lookup
from app.handlers.news import NewsError, _lookup as _news_lookup
from app.handlers.weather import WeatherError, _lookup as _weather_lookup

_WEATHER_EXTENSIONS = declare_mcp_discovery_extension(
    DeclareMcpDiscoveryConfig(
        tool_name="weather",
        description=ROUTE_DESCRIPTIONS["weather"],
        input_schema=WEATHER_INPUT_SCHEMA,
        example={"city": "Paris"},
        output=OutputConfig(example=WEATHER_SAMPLE_OUTPUT),
    )
)
_CRYPTO_EXTENSIONS = declare_mcp_discovery_extension(
    DeclareMcpDiscoveryConfig(
        tool_name="crypto",
        description=ROUTE_DESCRIPTIONS["crypto"],
        input_schema=CRYPTO_INPUT_SCHEMA,
        example={"coins": ["btc", "eth"], "vs_currency": "usd"},
        output=OutputConfig(example=CRYPTO_SAMPLE_OUTPUT),
    )
)
_NEWS_EXTENSIONS = declare_mcp_discovery_extension(
    DeclareMcpDiscoveryConfig(
        tool_name="news",
        description=ROUTE_DESCRIPTIONS["news"],
        input_schema=NEWS_INPUT_SCHEMA,
        example={"limit": 10},
        output=OutputConfig(example=NEWS_SAMPLE_OUTPUT),
    )
)


async def _run_weather(args: dict, payer: str | None) -> dict:
    body_excerpt = json.dumps(args)
    try:
        with Timer() as t:
            result = await _weather_lookup(args)
    except WeatherError as exc:
        db.log_request(
            route="weather", method="MCP", status="error", payer=payer,
            user_agent="mcp", body_excerpt=body_excerpt, error_reason=exc.reason,
        )
        raise ServiceError(exc.reason) from exc
    price = effective_price(payer, price_float(config.PRICE_WEATHER))
    db.log_request(
        route="weather", method="MCP", status="paid", latency_ms=t.elapsed_ms,
        amount_usdc=price, payer=payer, user_agent="mcp", body_excerpt=body_excerpt,
    )
    return {**result, "x402_receipt": make_receipt(None, "weather", t.elapsed_ms, price)}


async def _run_crypto(args: dict, payer: str | None) -> dict:
    body_excerpt = json.dumps(args)
    try:
        with Timer() as t:
            result = await _crypto_lookup(args)
    except CryptoError as exc:
        db.log_request(
            route="crypto", method="MCP", status="error", payer=payer,
            user_agent="mcp", body_excerpt=body_excerpt, error_reason=exc.reason,
        )
        raise ServiceError(exc.reason) from exc
    price = effective_price(payer, price_float(config.PRICE_CRYPTO))
    db.log_request(
        route="crypto", method="MCP", status="paid", latency_ms=t.elapsed_ms,
        amount_usdc=price, payer=payer, user_agent="mcp", body_excerpt=body_excerpt,
    )
    return {**result, "x402_receipt": make_receipt(None, "crypto", t.elapsed_ms, price)}


async def _run_news(args: dict, payer: str | None) -> dict:
    body_excerpt = json.dumps(args)
    try:
        with Timer() as t:
            result = await _news_lookup(args)
    except NewsError as exc:
        db.log_request(
            route="news", method="MCP", status="error", payer=payer,
            user_agent="mcp", body_excerpt=body_excerpt, error_reason=exc.reason,
        )
        raise ServiceError(exc.reason) from exc
    price = effective_price(payer, price_float(config.PRICE_NEWS))
    db.log_request(
        route="news", method="MCP", status="paid", latency_ms=t.elapsed_ms,
        amount_usdc=price, payer=payer, user_agent="mcp", body_excerpt=body_excerpt,
    )
    return {**result, "x402_receipt": make_receipt(None, "news", t.elapsed_ms, price)}


@mcp.tool(name="weather", description=ROUTE_DESCRIPTIONS["weather"])
async def weather_tool(
    city: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    ctx: Context = None,
) -> ToolResult:
    args = {k: v for k, v in (("city", city), ("lat", lat), ("lon", lon)) if v is not None}
    return await _paid_tool_call(
        tool_name="weather",
        route_key="POST /weather",
        ctx=ctx,
        args=args,
        extensions=_WEATHER_EXTENSIONS,
        run_and_log=_run_weather,
    )


@mcp.tool(name="crypto", description=ROUTE_DESCRIPTIONS["crypto"])
async def crypto_tool(
    coins: str | list[str],
    vs_currency: str = "usd",
    ctx: Context = None,
) -> ToolResult:
    return await _paid_tool_call(
        tool_name="crypto",
        route_key="POST /crypto",
        ctx=ctx,
        args={"coins": coins, "vs_currency": vs_currency},
        extensions=_CRYPTO_EXTENSIONS,
        run_and_log=_run_crypto,
    )


@mcp.tool(name="news", description=ROUTE_DESCRIPTIONS["news"])
async def news_tool(limit: int = 10, ctx: Context = None) -> ToolResult:
    return await _paid_tool_call(
        tool_name="news",
        route_key="POST /news",
        ctx=ctx,
        args={"limit": limit},
        extensions=_NEWS_EXTENSIONS,
        run_and_log=_run_news,
    )


# --- can-pay / probe ($0.001 preflight bait for first Bazaar settle) ---------

from app.handlers.can_pay import CanPayError, _lookup as _can_pay_lookup
from app.handlers.probe import ProbeError, _lookup as _probe_lookup

_CAN_PAY_EXTENSIONS = declare_mcp_discovery_extension(
    DeclareMcpDiscoveryConfig(
        tool_name="can_pay",
        description=ROUTE_DESCRIPTIONS["can-pay"],
        input_schema=CAN_PAY_INPUT_SCHEMA,
        example={"address": "0xb3F32bdfe8D07825BC0D7387295aB1D7559BA69d", "amount": 0.001},
        output=OutputConfig(example=CAN_PAY_SAMPLE_OUTPUT),
    )
)
_PROBE_EXTENSIONS = declare_mcp_discovery_extension(
    DeclareMcpDiscoveryConfig(
        tool_name="probe",
        description=ROUTE_DESCRIPTIONS["probe"],
        input_schema=PROBE_INPUT_SCHEMA,
        example={"url": "https://x402.agentindex.world/weather?city=Paris"},
        output=OutputConfig(example=PROBE_SAMPLE_OUTPUT),
    )
)


async def _run_can_pay(args: dict, payer: str | None) -> dict:
    body_excerpt = json.dumps(args)
    try:
        with Timer() as t:
            result = await _can_pay_lookup(args)
    except CanPayError as exc:
        db.log_request(
            route="can-pay", method="MCP", status="error", payer=payer,
            user_agent="mcp", body_excerpt=body_excerpt, error_reason=exc.reason,
        )
        raise ServiceError(exc.reason) from exc
    price = effective_price(payer, price_float(config.PRICE_CAN_PAY))
    db.log_request(
        route="can-pay", method="MCP", status="paid", latency_ms=t.elapsed_ms,
        amount_usdc=price, payer=payer, user_agent="mcp", body_excerpt=body_excerpt,
    )
    return {**result, "x402_receipt": make_receipt(None, "can-pay", t.elapsed_ms, price)}


async def _run_probe(args: dict, payer: str | None) -> dict:
    body_excerpt = json.dumps(args)
    try:
        with Timer() as t:
            result = await _probe_lookup(args)
    except ProbeError as exc:
        db.log_request(
            route="probe", method="MCP", status="error", payer=payer,
            user_agent="mcp", body_excerpt=body_excerpt, error_reason=exc.reason,
        )
        raise ServiceError(exc.reason) from exc
    price = effective_price(payer, price_float(config.PRICE_PROBE))
    db.log_request(
        route="probe", method="MCP", status="paid", latency_ms=t.elapsed_ms,
        amount_usdc=price, payer=payer, user_agent="mcp", body_excerpt=body_excerpt,
    )
    return {**result, "x402_receipt": make_receipt(None, "probe", t.elapsed_ms, price)}


@mcp.tool(name="can_pay", description=ROUTE_DESCRIPTIONS["can-pay"])
async def can_pay_tool(
    address: str,
    amount: float = 0.001,
    ctx: Context = None,
) -> ToolResult:
    return await _paid_tool_call(
        tool_name="can_pay",
        route_key="POST /can-pay",
        ctx=ctx,
        args={"address": address, "amount": amount},
        extensions=_CAN_PAY_EXTENSIONS,
        run_and_log=_run_can_pay,
    )


@mcp.tool(name="probe", description=ROUTE_DESCRIPTIONS["probe"])
async def probe_tool(
    url: str,
    method: str = "GET",
    ctx: Context = None,
) -> ToolResult:
    return await _paid_tool_call(
        tool_name="probe",
        route_key="POST /probe",
        ctx=ctx,
        args={"url": url, "method": method},
        extensions=_PROBE_EXTENSIONS,
        run_and_log=_run_probe,
    )

# --- multi-chain wallet / gas reads ($0.0001 acquisition routes) -----------

from app.handlers.gas_price import _lookup as _gas_price_lookup
from app.handlers.agent_health import _lookup as _agent_health_lookup
from app.handlers.wallet_balance import _lookup as _wallet_balance_lookup
from app.handlers.wallet_intelligence import _lookup as _wallet_intelligence_lookup
from app.upstream.evm_rpc import EvmRpcError

_WALLET_BALANCE_EXTENSIONS = declare_mcp_discovery_extension(
    DeclareMcpDiscoveryConfig(
        tool_name="wallet_balance",
        description=ROUTE_DESCRIPTIONS["wallet-balance"],
        input_schema=WALLET_BALANCE_INPUT_SCHEMA,
        example={
            "address": "0xb3F32bdfe8D07825BC0D7387295aB1D7559BA69d",
            "network": "base",
        },
        output=OutputConfig(example=WALLET_BALANCE_SAMPLE_OUTPUT),
    )
)
_GAS_PRICE_EXTENSIONS = declare_mcp_discovery_extension(
    DeclareMcpDiscoveryConfig(
        tool_name="gas_price",
        description=ROUTE_DESCRIPTIONS["gas-price"],
        input_schema=GAS_PRICE_INPUT_SCHEMA,
        example={"network": "base"},
        output=OutputConfig(example=GAS_PRICE_SAMPLE_OUTPUT),
    )
)
_WALLET_INTELLIGENCE_EXTENSIONS = declare_mcp_discovery_extension(
    DeclareMcpDiscoveryConfig(
        tool_name="wallet_intelligence",
        description=ROUTE_DESCRIPTIONS["wallet-intelligence"],
        input_schema=WALLET_INTELLIGENCE_INPUT_SCHEMA,
        example={
            "address": "0xb3F32bdfe8D07825BC0D7387295aB1D7559BA69d",
            "networks": ["base", "ethereum", "polygon", "arbitrum", "optimism"],
            "amount_usdc": 0.001,
        },
        output=OutputConfig(example=WALLET_INTELLIGENCE_SAMPLE_OUTPUT),
    )
)
_X402_ECHO_EXTENSIONS = declare_mcp_discovery_extension(
    DeclareMcpDiscoveryConfig(
        tool_name="x402_echo",
        description=ROUTE_DESCRIPTIONS["x402-echo"],
        input_schema=X402_ECHO_INPUT_SCHEMA,
        example={"message": "hello agent"},
        output=OutputConfig(example=X402_ECHO_SAMPLE_OUTPUT),
    )
)
_AGENT_HEALTH_EXTENSIONS = declare_mcp_discovery_extension(
    DeclareMcpDiscoveryConfig(
        tool_name="agent_health",
        description=ROUTE_DESCRIPTIONS["agent-health"],
        input_schema=AGENT_HEALTH_INPUT_SCHEMA,
        example={"url": "https://x402.agentindex.world/search", "method": "POST"},
        output=OutputConfig(example=AGENT_HEALTH_SAMPLE_OUTPUT),
    )
)


async def _run_wallet_balance(args: dict, payer: str | None) -> dict:
    body_excerpt = json.dumps(args)
    try:
        with Timer() as timer:
            result = await _wallet_balance_lookup(args)
    except EvmRpcError as exc:
        db.log_request(
            route="wallet-balance", method="MCP", status="error", payer=payer,
            user_agent="mcp", body_excerpt=body_excerpt, error_reason=str(exc),
        )
        raise ServiceError(str(exc)) from exc
    price = effective_price(payer, price_float(config.PRICE_WALLET_BALANCE))
    db.log_request(
        route="wallet-balance", method="MCP", status="paid",
        latency_ms=timer.elapsed_ms, amount_usdc=price, payer=payer,
        user_agent="mcp", body_excerpt=body_excerpt,
    )
    return {
        **result,
        "x402_receipt": make_receipt(None, "wallet-balance", timer.elapsed_ms, price),
    }


async def _run_gas_price(args: dict, payer: str | None) -> dict:
    body_excerpt = json.dumps(args)
    try:
        with Timer() as timer:
            result = await _gas_price_lookup(args)
    except EvmRpcError as exc:
        db.log_request(
            route="gas-price", method="MCP", status="error", payer=payer,
            user_agent="mcp", body_excerpt=body_excerpt, error_reason=str(exc),
        )
        raise ServiceError(str(exc)) from exc
    price = effective_price(payer, price_float(config.PRICE_GAS_PRICE))
    db.log_request(
        route="gas-price", method="MCP", status="paid",
        latency_ms=timer.elapsed_ms, amount_usdc=price, payer=payer,
        user_agent="mcp", body_excerpt=body_excerpt,
    )
    return {
        **result,
        "x402_receipt": make_receipt(None, "gas-price", timer.elapsed_ms, price),
    }

async def _run_wallet_intelligence(args: dict, payer: str | None) -> dict:
    body_excerpt = json.dumps(args)
    try:
        with Timer() as timer:
            result = await _wallet_intelligence_lookup(args)
    except EvmRpcError as exc:
        db.log_request(
            route="wallet-intelligence", method="MCP", status="error", payer=payer,
            user_agent="mcp", body_excerpt=body_excerpt, error_reason=str(exc),
        )
        raise ServiceError(str(exc)) from exc
    price = effective_price(payer, price_float(config.PRICE_WALLET_INTELLIGENCE))
    db.log_request(
        route="wallet-intelligence", method="MCP", status="paid",
        latency_ms=timer.elapsed_ms, amount_usdc=price, payer=payer,
        user_agent="mcp", body_excerpt=body_excerpt,
    )
    return {
        **result,
        "x402_receipt": make_receipt(
            None, "wallet-intelligence", timer.elapsed_ms, price
        ),
    }

async def _run_x402_echo(args: dict, payer: str | None) -> dict:
    body_excerpt = json.dumps(args)
    with Timer() as timer:
        result = {
            "ok": True,
            "purpose": "x402-mainnet-conformance",
            "network": config.X402_NETWORK,
            "price_usdc": price_float(config.PRICE_X402_ECHO),
            "price_atomic_usdc": "1",
            "payer": payer,
            "request": {"method": "MCP", "echo": args},
        }
    price = effective_price(payer, price_float(config.PRICE_X402_ECHO))
    db.log_request(
        route="x402-echo", method="MCP", status="paid",
        latency_ms=timer.elapsed_ms, amount_usdc=price, payer=payer,
        user_agent="mcp", body_excerpt=body_excerpt,
    )
    return {
        **result,
        "x402_receipt": make_receipt(None, "x402-echo", timer.elapsed_ms, price),
    }

async def _run_agent_health(args: dict, payer: str | None) -> dict:
    body_excerpt = json.dumps(args)
    with Timer() as timer:
        result = await _agent_health_lookup(args)
    price = effective_price(payer, price_float(config.PRICE_AGENT_HEALTH))
    db.log_request(
        route="agent-health", method="MCP", status="paid",
        latency_ms=timer.elapsed_ms, amount_usdc=price, payer=payer,
        user_agent="mcp", body_excerpt=body_excerpt,
    )
    return {
        **result,
        "x402_receipt": make_receipt(None, "agent-health", timer.elapsed_ms, price),
    }


@mcp.tool(name="wallet_balance", description=ROUTE_DESCRIPTIONS["wallet-balance"])
async def wallet_balance_tool(
    address: str,
    network: str = "base",
    ctx: Context = None,
) -> ToolResult:
    return await _paid_tool_call(
        tool_name="wallet_balance",
        route_key="POST /wallet-balance",
        ctx=ctx,
        args={"address": address, "network": network},
        extensions=_WALLET_BALANCE_EXTENSIONS,
        run_and_log=_run_wallet_balance,
    )


@mcp.tool(name="gas_price", description=ROUTE_DESCRIPTIONS["gas-price"])
async def gas_price_tool(
    network: str = "base",
    ctx: Context = None,
) -> ToolResult:
    return await _paid_tool_call(
        tool_name="gas_price",
        route_key="POST /gas-price",
        ctx=ctx,
        args={"network": network},
        extensions=_GAS_PRICE_EXTENSIONS,
        run_and_log=_run_gas_price,
    )

@mcp.tool(
    name="wallet_intelligence",
    description=ROUTE_DESCRIPTIONS["wallet-intelligence"],
)
async def wallet_intelligence_tool(
    address: str,
    networks: list[str] | None = None,
    amount_usdc: float = 0.001,
    ctx: Context = None,
) -> ToolResult:
    return await _paid_tool_call(
        tool_name="wallet_intelligence",
        route_key="POST /wallet-intelligence",
        ctx=ctx,
        args={
            "address": address,
            "networks": networks or [
                "base", "ethereum", "polygon", "arbitrum", "optimism"
            ],
            "amount_usdc": amount_usdc,
        },
        extensions=_WALLET_INTELLIGENCE_EXTENSIONS,
        run_and_log=_run_wallet_intelligence,
    )

@mcp.tool(name="x402_echo", description=ROUTE_DESCRIPTIONS["x402-echo"])
async def x402_echo_tool(
    message: str = "hello agent",
    ctx: Context = None,
) -> ToolResult:
    return await _paid_tool_call(
        tool_name="x402_echo",
        route_key="POST /x402-echo",
        ctx=ctx,
        args={"message": message},
        extensions=_X402_ECHO_EXTENSIONS,
        run_and_log=_run_x402_echo,
    )

@mcp.tool(name="agent_health", description=ROUTE_DESCRIPTIONS["agent-health"])
async def agent_health_tool(
    url: str,
    method: str = "GET",
    ctx: Context = None,
) -> ToolResult:
    return await _paid_tool_call(
        tool_name="agent_health",
        route_key="POST /agent-health",
        ctx=ctx,
        args={"url": url, "method": method},
        extensions=_AGENT_HEALTH_EXTENSIONS,
        run_and_log=_run_agent_health,
    )


# --- detect_language (GET /detect-language, free - no payment flow) --------

# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY @mcp.tool(
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY     name="detect_language",
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY     description=(
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY         "Detect the language of a piece of text - free, no payment. The "
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY         "kit's entry point: read_pdf, read_web_page, extract_structured "
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY         "and summarize do the same kind of work, paid."
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY     ),
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY )
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY async def detect_language_tool(text: str) -> dict:
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY     if not text or not text.strip():
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY         return {"error": {"reason": "missing_text"}}
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY     language, confidence = _language_identifier.classify(text)
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY     return {"text": text, "language": language, "confidence": round(float(confidence), 4)}





# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # --- tool_result digest (POST /tool-result-digest, free) --------------------

# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY from app.tool_digest import digest_tool_result as _digest_tool_result
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY from app.tool_digest import verify_tool_result_digest as _verify_tool_result_digest

# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY _TOOL_DIGEST_DESCRIPTION = (
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY     "Stable SHA-256 digest of an MCP tool_result so agents can verify payloads "
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY     "before settling payment — free, no account."
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY )


# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY @mcp.tool(name="digest_tool_result", description=_TOOL_DIGEST_DESCRIPTION)
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY async def digest_tool_result_tool(
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY     tool_name: str,
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY     content: str,
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY     tool_use_id: str | None = None,
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY ) -> dict:
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY     if not tool_name or not str(tool_name).strip():
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY         return {"error": {"reason": "missing_tool_name"}}
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY     if content is None or (isinstance(content, str) and not content.strip()):
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY         return {"error": {"reason": "missing_content"}}
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY     try:
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY         return _digest_tool_result(tool_name, content, tool_use_id=tool_use_id)
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY     except ValueError as exc:
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY         return {"error": {"reason": str(exc)}}


# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY @mcp.tool(
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY     name="verify_tool_result_digest",
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY     description=(
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY         "Recompute the canonical digest and return match=true/false — free. "
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY         "Use after a paid tool call to confirm the payload."
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY     ),
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY )
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY async def verify_tool_result_digest_tool(
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY     expected_digest: str,
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY     tool_name: str,
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY     content: str,
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY     tool_use_id: str | None = None,
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY ) -> dict:
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY     if not expected_digest or not str(expected_digest).strip():
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY         return {"error": {"reason": "missing_digest"}}
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY     if not tool_name or not str(tool_name).strip():
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY         return {"error": {"reason": "missing_tool_name"}}
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY     try:
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY         return _verify_tool_result_digest(
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY             expected_digest, tool_name, content, tool_use_id=tool_use_id
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY         )
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY     except ValueError as exc:
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY         return {"error": {"reason": str(exc)}}


# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # --- relationship memory (POST /relationship-memory/validate, free) ----------

# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY from app.relationship_memory import json_schema as _relationship_memory_schema
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY from app.relationship_memory import validate_card as _validate_relationship_card


# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY @mcp.tool(
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY     name="relationship_memory_schema",
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY     description=(
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY         "Return the JSON Schema for portable agent relationship memory cards (v1) — "
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY         "free. Remember interlocutors, not isolated messages."
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY     ),
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY )
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY async def relationship_memory_schema_tool() -> dict:
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY     return _relationship_memory_schema()


# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY @mcp.tool(
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY     name="validate_relationship_memory",
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY     description=(
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY         "Validate a relationship memory card against the v1 schema — free. "
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY         "Pass the card object as JSON."
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY     ),
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY )
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY async def validate_relationship_memory_tool(card: dict) -> dict:
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY     normalized, errors = _validate_relationship_card(card)
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY     return {
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY         "valid": not errors,
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY         "v": 1,
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY         "errors": errors,
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY         "normalized": normalized,
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY     }


# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # --- tool delivery receipt (POST /tool-delivery-receipt/validate, free) ------

# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY from app.tool_delivery_receipt import json_schema as _tool_delivery_receipt_schema
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY from app.tool_delivery_receipt import validate_receipt as _validate_tool_delivery_receipt


# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY @mcp.tool(
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY     name="tool_delivery_receipt_schema",
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY     description=(
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY         "Return the JSON Schema for portable tool delivery receipts (v1) — free. "
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY         "Link x402 payment metadata to a tool_result SHA-256 digest."
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY     ),
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY )
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY async def tool_delivery_receipt_schema_tool() -> dict:
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY     return _tool_delivery_receipt_schema()


# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY @mcp.tool(
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY     name="validate_tool_delivery_receipt",
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY     description=(
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY         "Validate a tool delivery receipt against the v1 schema — free. "
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY         "Pass receipt JSON; optional content re-verifies delivery.digest."
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY     ),
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY )
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY async def validate_tool_delivery_receipt_tool(
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY     receipt: dict,
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY     content: str | dict | list | None = None,
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY ) -> dict:
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY     normalized, errors, digest_check = _validate_tool_delivery_receipt(
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY         receipt, content=content
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY     )
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY     out: dict = {
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY         "valid": not errors,
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY         "v": 1,
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY         "errors": errors,
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY         "normalized": normalized,
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY     }
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY     if digest_check is not None:
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY         out["digest_verification"] = digest_check
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY     return out


# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # --- honest delivery refusal (POST /honest-delivery-refusal/validate, free) -

# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY from app.honest_delivery_refusal import json_schema as _honest_delivery_refusal_schema
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY from app.honest_delivery_refusal import validate_refusal as _validate_honest_delivery_refusal


# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY @mcp.tool(
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY     name="honest_delivery_refusal_schema",
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY     description=(
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY         "Return the JSON Schema for honest delivery refusals (v1) — free. "
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY         "Structured failure record when no tool_result can be delivered."
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY     ),
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY )
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY async def honest_delivery_refusal_schema_tool() -> dict:
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY     return _honest_delivery_refusal_schema()


# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY @mcp.tool(
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY     name="validate_honest_delivery_refusal",
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY     description="Validate an honest delivery refusal against the v1 schema — free.",
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY )
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY async def validate_honest_delivery_refusal_tool(refusal: dict) -> dict:
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY     normalized, errors = _validate_honest_delivery_refusal(refusal)
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY     return {
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY         "valid": not errors,
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY         "v": 1,
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY         "errors": errors,
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY         "normalized": normalized,
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY     }


# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # --- coordination thread (POST /coordination-thread/validate, free) ---------

# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY from app.coordination_thread import json_schema as _coordination_thread_schema
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY from app.coordination_thread import validate_turn as _validate_coordination_thread_turn


# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY @mcp.tool(
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY     name="coordination_thread_schema",
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY     description=(
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY         "Return the JSON Schema for coordination thread turns (v1) — free. "
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY         "One structured turn in a multi-agent conversation."
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY     ),
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY )
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY async def coordination_thread_schema_tool() -> dict:
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY     return _coordination_thread_schema()


# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY @mcp.tool(
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY     name="validate_coordination_thread_turn",
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY     description=(
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY         "Validate a coordination thread turn against the v1 schema — free. "
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY         "Pass the turn object as JSON."
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY     ),
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY )
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY async def validate_coordination_thread_turn_tool(turn: dict) -> dict:
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY     normalized, errors = _validate_coordination_thread_turn(turn)
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY     return {
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY         "valid": not errors,
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY         "v": 1,
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY         "errors": errors,
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY         "normalized": normalized,
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY     }


# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # --- return visit pledge (POST /return-visit-pledge/validate, free) -----------

# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY from app.return_visit_pledge import json_schema as _return_visit_pledge_schema
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY from app.return_visit_pledge import validate_pledge as _validate_return_visit_pledge


# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY @mcp.tool(
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY     name="return_visit_pledge_schema",
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY     description=(
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY         "Return the JSON Schema for return visit pledges (v1) — free. "
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY         "Commit to come back to a peer or thread by a deadline."
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY     ),
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY )
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY async def return_visit_pledge_schema_tool() -> dict:
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY     return _return_visit_pledge_schema()


# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY @mcp.tool(
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY     name="validate_return_visit_pledge",
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY     description=(
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY         "Validate a return visit pledge against the v1 schema — free. "
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY         "Pass the pledge object as JSON."
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY     ),
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY )
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY async def validate_return_visit_pledge_tool(pledge: dict) -> dict:
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY     normalized, errors = _validate_return_visit_pledge(pledge)
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY     return {
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY         "valid": not errors,
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY         "v": 1,
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY         "errors": errors,
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY         "normalized": normalized,
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY     }


# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY # --- coordination thread snapshot (POST /coordination-thread-snapshot/validate) -

# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY from app.coordination_snapshot import json_schema as _coordination_snapshot_schema
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY from app.coordination_snapshot import validate_snapshot as _validate_coordination_snapshot


# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY @mcp.tool(
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY     name="coordination_thread_snapshot_schema",
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY     description=(
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY         "Return the JSON Schema for coordination thread snapshots (v1) — free. "
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY         "Bundle turns, cards and open pledges for handoff between runs."
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY     ),
# PAID_ONLY # PAID_ONLY # PAID_ONLY # PAID_ONLY )
# PAID_ONLY # PAID_ONLY # PAID_ONLY async def coordination_thread_snapshot_schema_tool() -> dict:
# PAID_ONLY # PAID_ONLY # PAID_ONLY     return _coordination_snapshot_schema()


# PAID_ONLY # PAID_ONLY # PAID_ONLY @mcp.tool(
# PAID_ONLY # PAID_ONLY # PAID_ONLY     name="validate_coordination_thread_snapshot",
# PAID_ONLY # PAID_ONLY # PAID_ONLY     description=(
# PAID_ONLY # PAID_ONLY # PAID_ONLY         "Validate a coordination thread snapshot against the v1 schema — free. "
# PAID_ONLY # PAID_ONLY # PAID_ONLY         "Pass the snapshot object as JSON."
# PAID_ONLY # PAID_ONLY # PAID_ONLY     ),
# PAID_ONLY # PAID_ONLY # PAID_ONLY )
# PAID_ONLY # PAID_ONLY async def validate_coordination_thread_snapshot_tool(snapshot: dict) -> dict:
# PAID_ONLY # PAID_ONLY     normalized, errors = _validate_coordination_snapshot(snapshot)
# PAID_ONLY # PAID_ONLY     return {
# PAID_ONLY # PAID_ONLY         "valid": not errors,
# PAID_ONLY # PAID_ONLY         "v": 1,
# PAID_ONLY # PAID_ONLY         "errors": errors,
# PAID_ONLY # PAID_ONLY         "normalized": normalized,
# PAID_ONLY # PAID_ONLY     }


# PAID_ONLY # PAID_ONLY # --- agent trust kit manifest (GET /.well-known/agent-trust-kit.json, free) ---

# PAID_ONLY # PAID_ONLY from app.agent_trust_kit import manifest as _agent_trust_kit_manifest


# PAID_ONLY # PAID_ONLY @mcp.tool(
# PAID_ONLY # PAID_ONLY     name="get_agent_trust_kit",
# PAID_ONLY # PAID_ONLY     description=(
# PAID_ONLY # PAID_ONLY         "Return the agent trust kit manifest (v1) — free index of JSON schemas, "
# PAID_ONLY # PAID_ONLY         "HTTP validators, MCP tool names and suggested buyer/coordination workflows."
# PAID_ONLY # PAID_ONLY     ),
# PAID_ONLY # PAID_ONLY )
# PAID_ONLY async def get_agent_trust_kit_tool() -> dict:
# PAID_ONLY     return _agent_trust_kit_manifest()


# PAID_ONLY # --- discover_mcp_servers (GET /discover, free - no payment flow) ----------
# PAID_ONLY # Meme logique que app/handlers/discover.py, exposee ici pour que les clients
# PAID_ONLY # MCP qui listent tools/list la trouvent sans connaitre la route HTTP - le
# PAID_ONLY # chantier ouvert sur /discover manque de trafic reel, pas de code : les
# PAID_ONLY # scanners MCP (AIVE-MCP-Discover, 402explorer, vus dans les journaux nginx)
# PAID_ONLY # tapent deja /mcp, jamais /discover en HTTP nu.



# PAID_ONLY # --- discover_semantic (POST /discover, paid - local MCP snapshot) -----------
# PAID_ONLY # Les scanners MCP tapent /mcp mais pas POST /discover en HTTP nu (mesure
# PAID_ONLY # nginx 17/09). Meme surface x402 que la route HTTP.

# PAID_ONLY _DISCOVER_SEMANTIC_EXTENSIONS = declare_mcp_discovery_extension(
# PAID_ONLY     DeclareMcpDiscoveryConfig(
# PAID_ONLY         tool_name="discover_semantic",
# PAID_ONLY         description=ROUTE_DESCRIPTIONS["discover"],
# PAID_ONLY         input_schema=DISCOVER_INPUT_SCHEMA,
# PAID_ONLY         example={"q": "read a PDF and give me markdown"},
# PAID_ONLY         output=OutputConfig(example=DISCOVER_SAMPLE_OUTPUT),
# PAID_ONLY     )
# PAID_ONLY )


# PAID_ONLY async def _run_discover_semantic_paid(args: dict, payer: str | None) -> dict:
# PAID_ONLY     body_excerpt = json.dumps(args)
# PAID_ONLY     q = args.get("q")
# PAID_ONLY     if not isinstance(q, str) or not q.strip():
# PAID_ONLY         db.log_request(
# PAID_ONLY             route="discover", method="MCP", status="error", payer=payer,
# PAID_ONLY             user_agent="mcp", body_excerpt=body_excerpt, error_reason="missing_q",
# PAID_ONLY         )
# PAID_ONLY         raise ServiceError("missing_q")

# PAID_ONLY     max_results = args.get("max_results", 5)
# PAID_ONLY     if not isinstance(max_results, int) or isinstance(max_results, bool):
# PAID_ONLY         max_results = 5
# PAID_ONLY     max_results = max(1, min(max_results, DISCOVER_MAX_RESULTS))

# PAID_ONLY     threshold = args.get("min_similarity", 0.30)
# PAID_ONLY     if not isinstance(threshold, int | float) or isinstance(threshold, bool):
# PAID_ONLY         threshold = 0.30
# PAID_ONLY     threshold = float(max(0.0, min(threshold, 1.0)))

# PAID_ONLY     try:
# PAID_ONLY         with Timer() as t:
# PAID_ONLY             result = await _run_discover_semantic(q.strip()[:500], max_results, threshold)
# PAID_ONLY     except OllamaError as exc:
# PAID_ONLY         db.log_request(
# PAID_ONLY             route="discover", method="MCP", status="error", payer=payer,
# PAID_ONLY             user_agent="mcp", body_excerpt=body_excerpt, error_reason=str(exc)[:200],
# PAID_ONLY         )
# PAID_ONLY         raise ServiceError("upstream_error", detail=str(exc)[:200]) from exc

# PAID_ONLY     price = effective_price(payer, price_float(config.PRICE_DISCOVER))
# PAID_ONLY     db.log_request(
# PAID_ONLY         route="discover", method="MCP", status="paid", latency_ms=t.elapsed_ms,
# PAID_ONLY         amount_usdc=price, payer=payer, user_agent="mcp", body_excerpt=body_excerpt,
# PAID_ONLY     )
# PAID_ONLY     receipt = make_receipt(None, "nomic-embed-text", t.elapsed_ms, price)
# PAID_ONLY     return {**result, "x402_receipt": receipt}


# PAID_ONLY @mcp.tool(name="discover_semantic", description=ROUTE_DESCRIPTIONS["discover"])
# PAID_ONLY async def discover_semantic_tool(
# PAID_ONLY     q: str,
# PAID_ONLY     max_results: int = 5,
# PAID_ONLY     min_similarity: float = 0.30,
# PAID_ONLY     ctx: Context = None,
# PAID_ONLY ) -> ToolResult:
# PAID_ONLY     args = {"q": q, "max_results": max_results, "min_similarity": min_similarity}
# PAID_ONLY     return await _paid_tool_call(
# PAID_ONLY         tool_name="discover_semantic",
# PAID_ONLY         route_key="POST /discover",
# PAID_ONLY         ctx=ctx,
# PAID_ONLY         args=args,
# PAID_ONLY         extensions=_DISCOVER_SEMANTIC_EXTENSIONS,
# PAID_ONLY         run_and_log=_run_discover_semantic_paid,
# PAID_ONLY     )


# PAID_ONLY @mcp.tool(
# PAID_ONLY     name="discover_mcp_servers",
# PAID_ONLY     description=DISCOVER_DESCRIPTION,
# PAID_ONLY )
async def discover_mcp_servers_tool(q: str, max_results: int = 5) -> dict:
    if not q or not q.strip():
        return {"error": {"reason": "missing_q"}}
    max_results = max(1, min(max_results, DISCOVER_MAX_RESULTS))
    try:
        result = await _run_discover_snapshot(q.strip()[:500], max_results, MIN_SIMILARITY)
    except OllamaError as exc:
        return {"error": {"reason": "upstream_error", "detail": str(exc)[:200]}}
    db.log_request(
        route="discover", method="MCP", status="unpaid", user_agent="mcp", body_excerpt=q[:2048],
    )
    base = config.BASE_URL.rstrip("/")
    return {**result, "paid_upgrade": _paid_upgrade_hint(base)}


# --- welcome salon + contact (free, MCP front door) ------------------------

from app.handlers.accueil import _payload as _accueil_payload
from app.handlers.contact import ContactIn, Declaration, deposer_contact, lire_contact

_ACCUEIL_MCP_DESC = (
    "Kairos welcome salon — same JSON as GET /accueil. Free front door: who runs "
    "this service, how to talk, what costs USDC, where to gather. Start here before "
    "paid routes."
)

_CONTACT_MCP_DESC = (
    "Write to Kairos and optionally DECLARE yourself (what_i_do, endpoint, skills) — "
    "free, no payment. Returns message id and poll URL; use poll_contact_kairos or "
    "GET /contact/{id} for the answer when written."
)


# PAID_ONLY @mcp.tool(name="get_welcome_salon", description=_ACCUEIL_MCP_DESC)
async def get_welcome_salon_tool() -> dict:
    return _accueil_payload()


# PAID_ONLY @mcp.tool(name="contact_kairos", description=_CONTACT_MCP_DESC)
async def contact_kairos_tool(
    sender: str,
    subject: str,
    body: str,
    reply_to: str | None = None,
    declares_what_i_do: str | None = None,
    declares_endpoint: str | None = None,
    declares_skills: list[str] | None = None,
) -> dict:
    declares = None
    if declares_what_i_do:
        declares = Declaration(
            what_i_do=declares_what_i_do,
            endpoint=declares_endpoint,
            skills=declares_skills,
        )
    try:
        payload = ContactIn(
            sender=sender,
            subject=subject,
            body=body,
            reply_to=reply_to,
            declares=declares,
        )
    except ValueError as exc:
        return {"error": "invalid_payload", "detail": str(exc)[:300]}
    corps, code = deposer_contact(payload, user_agent="mcp", from_ip="mcp")
    if code >= 400:
        return {**corps, "http_status": code}
    db.log_request(
        route="contact", method="MCP", status="free", user_agent="mcp",
        body_excerpt=subject[:200],
    )
    return corps


# PAID_ONLY @mcp.tool(name="poll_contact_kairos", description=_CONTACT_MCP_DESC)
async def poll_contact_kairos_tool(message_id: str) -> dict:
    corps, code = lire_contact(message_id[:32])
    if code == 404:
        return corps
    return corps


# --- agent mesh board (GET /mesh, free) -------------------------------------

from app.handlers.agent_mesh import (
    BountyIn,
    ClaimIn,
    NodeIn,
    claim_bounty,
    get_bounty,
    ledger,
    list_bounties,
    list_nodes,
    post_bounty,
    register_node,
)

_MESH_DESC = (
    "Kairos agent mesh board — register nodes, post open bounties, claim work. "
    "Free. Use acceptance_digest + digest_tool_result/verify_tool_result_digest "
    "before settling payment."
)


# PAID_ONLY @mcp.tool(name="mesh_list_nodes", description=_MESH_DESC)
async def mesh_list_nodes_tool(limit: int = 20) -> dict:
    return list_nodes(max(1, min(limit, 50)))


# PAID_ONLY @mcp.tool(name="mesh_list_bounties", description=_MESH_DESC)
async def mesh_list_bounties_tool(status: str = "open", limit: int = 20) -> dict:
    return list_bounties(status, max(1, min(limit, 50)))


# PAID_ONLY @mcp.tool(name="mesh_register_node", description=_MESH_DESC)
async def mesh_register_node_tool(
    name: str,
    endpoint: str,
    skills: list[str] | None = None,
    about: str = "",
) -> dict:
    try:
        return register_node(
            NodeIn(name=name, endpoint=endpoint, skills=skills or [], about=about)
        )
    except ValueError as exc:
        return {"error": str(exc)}


# PAID_ONLY @mcp.tool(name="mesh_post_bounty", description=_MESH_DESC)
async def mesh_post_bounty_tool(
    poster_name: str,
    title: str,
    criteria: str,
    poster_endpoint: str = "",
    reward_usdc: float | None = None,
    acceptance_digest: str | None = None,
) -> dict:
    try:
        return post_bounty(
            BountyIn(
                poster_name=poster_name,
                poster_endpoint=poster_endpoint,
                title=title,
                criteria=criteria,
                reward_usdc=reward_usdc,
                acceptance_digest=acceptance_digest,
            )
        )
    except ValueError as exc:
        return {"error": str(exc)}


# PAID_ONLY @mcp.tool(name="mesh_claim_bounty", description=_MESH_DESC)
async def mesh_claim_bounty_tool(
    bounty_id: str,
    claimer_name: str,
    claimer_endpoint: str = "",
    note: str = "",
) -> dict:
    try:
        return claim_bounty(
            bounty_id,
            ClaimIn(claimer_name=claimer_name, claimer_endpoint=claimer_endpoint, note=note),
        )
    except LookupError:
        return {"error": "not_found"}
    except ValueError as exc:
        return {"error": str(exc)}


# PAID_ONLY @mcp.tool(name="mesh_ledger", description=_MESH_DESC)
async def mesh_ledger_tool(limit: int = 20) -> dict:
    return ledger(max(1, min(limit, 50)))


# --- usine-generated routes (app/generated/) ---------------------------

async def _run_generated(spec: RouteSpec, args: dict, payer: str | None) -> dict:
    """Same verify -> run -> settle shape as the 3 hand-built tools above,
    but delegating the actual work to the generic HTTP-proxy handler
    (app/generated/proxy_handler.py) so a usine-generated route's MCP
    surface and HTTP surface never diverge."""
    body_excerpt = json.dumps(args)
    try:
        with Timer() as t:
            result = await call_upstream(spec, args)
    except Exception as exc:
        db.log_request(
            route=spec.slug, method="MCP", status="error", payer=payer,
            user_agent="mcp", body_excerpt=body_excerpt, error_reason=str(exc)[:200],
        )
        raise ServiceError("upstream_error", detail=str(exc)[:200]) from exc

    price = effective_price(payer, price_float(spec.price))
    db.log_request(
        route=spec.slug, method="MCP", status="paid", latency_ms=t.elapsed_ms, amount_usdc=price,
        payer=payer, user_agent="mcp", body_excerpt=body_excerpt,
    )
    receipt = make_receipt(None, "http_proxy", t.elapsed_ms, price)
    return {**result, "x402_receipt": receipt}


def _register_generated_tool(spec: RouteSpec) -> None:
    route_key = f"POST /{spec.slug}"
    extensions = declare_mcp_discovery_extension(
        DeclareMcpDiscoveryConfig(
            tool_name=spec.slug,
            description=spec.description,
            input_schema=spec.input_schema,
            example=spec.upstream.get("sample_body", {}),
            output=OutputConfig(schema=spec.output_schema),
        )
    )

    # FastMCP binds arguments against the function's REAL signature, not
    # against `.parameters` (confirmed empirically - overriding `.parameters`
    # alone changes only what `tools/list` advertises, not what a call
    # actually validates against). A route's real input schema is only known
    # at runtime from the registry, so the input is wrapped under a single
    # `payload` object rather than synthesizing a matching Python signature
    # per route - `.parameters` below still advertises the true wrapped
    # shape, never a schema the tool doesn't actually accept.
    async def handler(payload: dict, ctx: Context = None) -> ToolResult:
        return await _paid_tool_call(
            tool_name=spec.slug,
            route_key=route_key,
            ctx=ctx,
            args=payload,
            extensions=extensions,
            run_and_log=lambda args, payer: _run_generated(spec, args, payer),
        )

    tool = FunctionTool.from_function(handler, name=spec.slug, description=spec.description)
    tool.parameters = {
        "type": "object",
        "properties": {"payload": spec.input_schema},
        "required": ["payload"],
    }
    mcp.add_tool(tool)


for _spec in live_routes():
    if _spec.handler_type == "http_proxy":
        _register_generated_tool(_spec)
