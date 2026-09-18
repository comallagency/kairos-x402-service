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
    MAX_BATCH_QUERIES,
    _clamp_max_results,
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
    EXTRACT_INPUT_SCHEMA,
    EXTRACT_SAMPLE_OUTPUT,
    FACT_CHECK_INPUT_SCHEMA,
    FACT_CHECK_SAMPLE_OUTPUT,
    JOBS_INPUT_SCHEMA,
    JOBS_SAMPLE_OUTPUT,
    KIT_TAGLINE,
    PDF_INPUT_SCHEMA,
    PDF_SAMPLE_OUTPUT,
    ROUTE_DESCRIPTIONS,
    SEARCH_INPUT_SCHEMA,
    SEARCH_SAMPLE_OUTPUT,
    SUMMARIZE_INPUT_SCHEMA,
    SUMMARIZE_SAMPLE_OUTPUT,
    TRANSLATE_INPUT_SCHEMA,
    TRANSLATE_SAMPLE_OUTPUT,
    WEB_READ_INPUT_SCHEMA,
    WEB_READ_SAMPLE_OUTPUT,
    DISCOVER_INPUT_SCHEMA,
    DISCOVER_SAMPLE_OUTPUT,
    build_route_configs,
    get_resource_server,
)

logger = logging.getLogger("x402.mcp_server")

mcp = FastMCP(
    "AgentIndex x402",
    instructions=(
        f"{KIT_TAGLINE} Paid per call in USDC on Base (x402/MPP) - no "
        "account, no API key. Start with detect_language (free) to confirm "
        "access, then read_pdf, read_web_page, extract_structured and "
        "summarize for the rest of the kit. Two more tools outside the "
        "kit: translate (batch translation) and jobs (delegated "
        "multi-step research, async). Also free, unrelated to the kit: "
        "digest_tool_result and verify_tool_result_digest (free integrity), discover_mcp_servers (free, snapshot-ranked) and discover_semantic "
        "(paid, snapshot embeddings) find MCP servers by need."
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
    summarize = bool(args.get("summarize", False))

    try:
        with Timer() as t:
            if is_batch:
                results, model_served = await _run_batch_search(query, max_results)
                summary_label = "; ".join(query)
            else:
                results, model_served = await run_web_search(query, max_results)
                summary_label = query
            summary = await _summarize(summary_label, results) if summarize else None
    except OpenRouterError as exc:
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
# @mcp.tool(name="search", description=ROUTE_DESCRIPTIONS["search"])
async def search_tool(
    query: str | list[str],
    max_results: int = 5,
    extract: bool = True,
    summarize: bool = False,
    ctx: Context = None,
) -> ToolResult:
    args = {"query": query, "max_results": max_results, "extract": extract, "summarize": summarize}
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
# @mcp.tool(name="fact_check", description=ROUTE_DESCRIPTIONS["fact-check"])
async def fact_check_tool(claim: str, ctx: Context = None) -> ToolResult:
    return await _paid_tool_call(
        tool_name="fact_check",
        route_key="POST /fact-check",
        ctx=ctx,
        args={"claim": claim},
        extensions=_FACT_CHECK_EXTENSIONS,
        run_and_log=_run_fact_check,
    )


# --- detect_language (GET /detect-language, free - no payment flow) --------

@mcp.tool(
    name="detect_language",
    description=(
        "Detect the language of a piece of text - free, no payment. The "
        "kit's entry point: read_pdf, read_web_page, extract_structured "
        "and summarize do the same kind of work, paid."
    ),
)
async def detect_language_tool(text: str) -> dict:
    if not text or not text.strip():
        return {"error": {"reason": "missing_text"}}
    language, confidence = _language_identifier.classify(text)
    return {"text": text, "language": language, "confidence": round(float(confidence), 4)}





# --- tool_result digest (POST /tool-result-digest, free) --------------------

from app.tool_digest import digest_tool_result as _digest_tool_result
from app.tool_digest import verify_tool_result_digest as _verify_tool_result_digest

_TOOL_DIGEST_DESCRIPTION = (
    "Stable SHA-256 digest of an MCP tool_result so agents can verify payloads "
    "before settling payment — free, no account."
)


@mcp.tool(name="digest_tool_result", description=_TOOL_DIGEST_DESCRIPTION)
async def digest_tool_result_tool(
    tool_name: str,
    content: str,
    tool_use_id: str | None = None,
) -> dict:
    if not tool_name or not str(tool_name).strip():
        return {"error": {"reason": "missing_tool_name"}}
    if content is None or (isinstance(content, str) and not content.strip()):
        return {"error": {"reason": "missing_content"}}
    try:
        return _digest_tool_result(tool_name, content, tool_use_id=tool_use_id)
    except ValueError as exc:
        return {"error": {"reason": str(exc)}}


@mcp.tool(
    name="verify_tool_result_digest",
    description=(
        "Recompute the canonical digest and return match=true/false — free. "
        "Use after a paid tool call to confirm the payload."
    ),
)
async def verify_tool_result_digest_tool(
    expected_digest: str,
    tool_name: str,
    content: str,
    tool_use_id: str | None = None,
) -> dict:
    if not expected_digest or not str(expected_digest).strip():
        return {"error": {"reason": "missing_digest"}}
    if not tool_name or not str(tool_name).strip():
        return {"error": {"reason": "missing_tool_name"}}
    try:
        return _verify_tool_result_digest(
            expected_digest, tool_name, content, tool_use_id=tool_use_id
        )
    except ValueError as exc:
        return {"error": {"reason": str(exc)}}


# --- discover_mcp_servers (GET /discover, free - no payment flow) ----------
# Meme logique que app/handlers/discover.py, exposee ici pour que les clients
# MCP qui listent tools/list la trouvent sans connaitre la route HTTP - le
# chantier ouvert sur /discover manque de trafic reel, pas de code : les
# scanners MCP (AIVE-MCP-Discover, 402explorer, vus dans les journaux nginx)
# tapent deja /mcp, jamais /discover en HTTP nu.



# --- discover_semantic (POST /discover, paid - local MCP snapshot) -----------
# Les scanners MCP tapent /mcp mais pas POST /discover en HTTP nu (mesure
# nginx 17/09). Meme surface x402 que la route HTTP.

_DISCOVER_SEMANTIC_EXTENSIONS = declare_mcp_discovery_extension(
    DeclareMcpDiscoveryConfig(
        tool_name="discover_semantic",
        description=ROUTE_DESCRIPTIONS["discover"],
        input_schema=DISCOVER_INPUT_SCHEMA,
        example={"q": "read a PDF and give me markdown"},
        output=OutputConfig(example=DISCOVER_SAMPLE_OUTPUT),
    )
)


async def _run_discover_semantic_paid(args: dict, payer: str | None) -> dict:
    body_excerpt = json.dumps(args)
    q = args.get("q")
    if not isinstance(q, str) or not q.strip():
        db.log_request(
            route="discover", method="MCP", status="error", payer=payer,
            user_agent="mcp", body_excerpt=body_excerpt, error_reason="missing_q",
        )
        raise ServiceError("missing_q")

    max_results = args.get("max_results", 5)
    if not isinstance(max_results, int) or isinstance(max_results, bool):
        max_results = 5
    max_results = max(1, min(max_results, DISCOVER_MAX_RESULTS))

    threshold = args.get("min_similarity", 0.30)
    if not isinstance(threshold, int | float) or isinstance(threshold, bool):
        threshold = 0.30
    threshold = float(max(0.0, min(threshold, 1.0)))

    try:
        with Timer() as t:
            result = await _run_discover_semantic(q.strip()[:500], max_results, threshold)
    except OllamaError as exc:
        db.log_request(
            route="discover", method="MCP", status="error", payer=payer,
            user_agent="mcp", body_excerpt=body_excerpt, error_reason=str(exc)[:200],
        )
        raise ServiceError("upstream_error", detail=str(exc)[:200]) from exc

    price = effective_price(payer, price_float(config.PRICE_DISCOVER))
    db.log_request(
        route="discover", method="MCP", status="paid", latency_ms=t.elapsed_ms,
        amount_usdc=price, payer=payer, user_agent="mcp", body_excerpt=body_excerpt,
    )
    receipt = make_receipt(None, "nomic-embed-text", t.elapsed_ms, price)
    return {**result, "x402_receipt": receipt}


@mcp.tool(name="discover_semantic", description=ROUTE_DESCRIPTIONS["discover"])
async def discover_semantic_tool(
    q: str,
    max_results: int = 5,
    min_similarity: float = 0.30,
    ctx: Context = None,
) -> ToolResult:
    args = {"q": q, "max_results": max_results, "min_similarity": min_similarity}
    return await _paid_tool_call(
        tool_name="discover_semantic",
        route_key="POST /discover",
        ctx=ctx,
        args=args,
        extensions=_DISCOVER_SEMANTIC_EXTENSIONS,
        run_and_log=_run_discover_semantic_paid,
    )


@mcp.tool(
    name="discover_mcp_servers",
    description=DISCOVER_DESCRIPTION,
)
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
    return result



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


@mcp.tool(name="mesh_list_nodes", description=_MESH_DESC)
async def mesh_list_nodes_tool(limit: int = 20) -> dict:
    return list_nodes(max(1, min(limit, 50)))


@mcp.tool(name="mesh_list_bounties", description=_MESH_DESC)
async def mesh_list_bounties_tool(status: str = "open", limit: int = 20) -> dict:
    return list_bounties(status, max(1, min(limit, 50)))


@mcp.tool(name="mesh_register_node", description=_MESH_DESC)
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


@mcp.tool(name="mesh_post_bounty", description=_MESH_DESC)
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


@mcp.tool(name="mesh_claim_bounty", description=_MESH_DESC)
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


@mcp.tool(name="mesh_ledger", description=_MESH_DESC)
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
