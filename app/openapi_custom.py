"""Post-processes FastAPI's auto-generated OpenAPI schema to add agent-discovery
metadata Circle's readiness scanner looks for (x-payment-info, a documented 402
response, info.x-guidance, externalDocs). Pure documentation - no payment logic
here; the runtime challenge is already correct and CDP-validated (see
BRIEF-CORRECTIONS.md).

Everything price/network/payTo-related is derived from build_route_configs()
(the same RouteConfig objects that build the real 402 challenge), never
hand-copied - that's exactly the class of bug that caused the tags to go
missing from the well-known earlier (see BRIEF-CORRECTIONS.md).

Scope, on explicit instruction: x402 only (no "mpp" protocol entry - the
mpp_attempts_24h dashboard counter has stayed at 0, nothing to build for),
single network (Base only, no multi-chain accepts - never asked for).
"""

from fastapi.openapi.utils import get_openapi
from x402.mechanisms.evm.default_assets import get_default_asset

from app import config
from app.purecalc.registry import COMPUTE_SPECS, inline_json_schema
from app.base_chain.registry import BASE_RPC_SPECS
from app.handlers.llm_gateway import MIN_SETTLE_USD
from app.generated.registry import live_routes
from app.x402_setup import (
    CRYPTO_INPUT_SCHEMA,
    CRYPTO_OUTPUT_SCHEMA,
    EXTRACT_INPUT_SCHEMA,
    EXTRACT_OUTPUT_SCHEMA,
    FACT_CHECK_INPUT_SCHEMA,
    FACT_CHECK_OUTPUT_SCHEMA,
    JOBS_INPUT_SCHEMA,
    JOBS_OUTPUT_SCHEMA,
    LLM_GATEWAY_INPUT_SCHEMA,
    LLM_GATEWAY_OUTPUT_SCHEMA,
    LLM_PER_MODEL_INPUT_SCHEMA,
    TOKEN_RISK_INPUT_SCHEMA,
    TOKEN_RISK_OUTPUT_SCHEMA,
    RESEARCH_INPUT_SCHEMA,
    RESEARCH_OUTPUT_SCHEMA,
    SENTIMENT_INPUT_SCHEMA,
    SENTIMENT_OUTPUT_SCHEMA,
    CLASSIFY_INPUT_SCHEMA,
    CLASSIFY_OUTPUT_SCHEMA,
    INTENT_INPUT_SCHEMA,
    INTENT_OUTPUT_SCHEMA,
    SPAM_CHECK_INPUT_SCHEMA,
    SPAM_CHECK_OUTPUT_SCHEMA,
    TOXICITY_INPUT_SCHEMA,
    TOXICITY_OUTPUT_SCHEMA,
    LANGUAGE_INPUT_SCHEMA,
    LANGUAGE_OUTPUT_SCHEMA,
    PII_CHECK_INPUT_SCHEMA,
    PII_CHECK_OUTPUT_SCHEMA,
    NEWS_INPUT_SCHEMA,
    NEWS_OUTPUT_SCHEMA,
    PDF_INPUT_SCHEMA,
    PDF_OUTPUT_SCHEMA,
    ROUTE_SUMMARIES,
    ROUTE_USE_CASES,
    DISCOVER_INPUT_EXAMPLE,
    DISCOVER_INPUT_SCHEMA,
    DISCOVER_OUTPUT_SCHEMA,
    DISCOVER_SAMPLE_OUTPUT,
    SEARCH_INPUT_SCHEMA,
    SEARCH_OUTPUT_SCHEMA,
    SUMMARIZE_INPUT_SCHEMA,
    SUMMARIZE_OUTPUT_SCHEMA,
    TRANSLATE_INPUT_SCHEMA,
    TRANSLATE_OUTPUT_SCHEMA,
    WEATHER_INPUT_SCHEMA,
    WEATHER_OUTPUT_SCHEMA,
    WEB_READ_INPUT_SCHEMA,
    WEB_READ_OUTPUT_SCHEMA,
    CAN_PAY_INPUT_SCHEMA,
    CAN_PAY_OUTPUT_SCHEMA,
    PROBE_INPUT_SCHEMA,
    PROBE_OUTPUT_SCHEMA,
    WALLET_BALANCE_INPUT_SCHEMA,
    WALLET_BALANCE_OUTPUT_SCHEMA,
    GAS_PRICE_INPUT_SCHEMA,
    GAS_PRICE_OUTPUT_SCHEMA,
    WALLET_INTELLIGENCE_INPUT_SCHEMA,
    WALLET_INTELLIGENCE_OUTPUT_SCHEMA,
    X402_ECHO_INPUT_SCHEMA,
    X402_ECHO_OUTPUT_SCHEMA,
    AGENT_HEALTH_INPUT_SCHEMA,
    AGENT_HEALTH_OUTPUT_SCHEMA,
    DECIDE_INPUT_SCHEMA,
    DECIDE_OUTPUT_SCHEMA,
    GUARD_INPUT_SCHEMA,
    GUARD_OUTPUT_SCHEMA,
    VERIFY_INPUT_SCHEMA,
    VERIFY_OUTPUT_SCHEMA,
    RANK_INPUT_SCHEMA,
    RANK_OUTPUT_SCHEMA,
    build_route_configs,
)

try:
    from app.x402_setup import MONTAGE_INPUT_SCHEMA, MONTAGE_OUTPUT_SCHEMA
except ImportError:
    MONTAGE_INPUT_SCHEMA = {"type": "object", "properties": {"brief": {"type": "string"}}}
    MONTAGE_OUTPUT_SCHEMA = {"type": "object"}

_INPUT_SCHEMAS = {
    "discover": DISCOVER_INPUT_SCHEMA,
    "search": SEARCH_INPUT_SCHEMA,
    "translate": TRANSLATE_INPUT_SCHEMA,
    "jobs": JOBS_INPUT_SCHEMA,
    "pdf": PDF_INPUT_SCHEMA,
    "web-read": WEB_READ_INPUT_SCHEMA,
    "extract": EXTRACT_INPUT_SCHEMA,
    "summarize": SUMMARIZE_INPUT_SCHEMA,
    "fact-check": FACT_CHECK_INPUT_SCHEMA,
    "weather": WEATHER_INPUT_SCHEMA,
    "crypto": CRYPTO_INPUT_SCHEMA,
    "news": NEWS_INPUT_SCHEMA,
    "can-pay": CAN_PAY_INPUT_SCHEMA,
    "probe": PROBE_INPUT_SCHEMA,
    "wallet-balance": WALLET_BALANCE_INPUT_SCHEMA,
    "gas-price": GAS_PRICE_INPUT_SCHEMA,
    "v1/chat/completions": LLM_GATEWAY_INPUT_SCHEMA,
    "llm/claude-sonnet": LLM_PER_MODEL_INPUT_SCHEMA,
    "llm/gpt-mini": LLM_PER_MODEL_INPUT_SCHEMA,
    "llm/llama": LLM_PER_MODEL_INPUT_SCHEMA,
    "llm/gemini-flash": LLM_PER_MODEL_INPUT_SCHEMA,
    "llm/deepseek": LLM_PER_MODEL_INPUT_SCHEMA,
    "token-risk": TOKEN_RISK_INPUT_SCHEMA,
    "research": RESEARCH_INPUT_SCHEMA,
    "sentiment": SENTIMENT_INPUT_SCHEMA,
    "classify": CLASSIFY_INPUT_SCHEMA,
    "intent": INTENT_INPUT_SCHEMA,
    "spam-check": SPAM_CHECK_INPUT_SCHEMA,
    "toxicity": TOXICITY_INPUT_SCHEMA,
    "language": LANGUAGE_INPUT_SCHEMA,
    "pii-check": PII_CHECK_INPUT_SCHEMA,
    "wallet-intelligence": WALLET_INTELLIGENCE_INPUT_SCHEMA,
    "x402-echo": X402_ECHO_INPUT_SCHEMA,
    "tip": X402_ECHO_INPUT_SCHEMA,
    "agent-claim": {
        "properties": {
            "url": {"type": "string"},
            "name": {"type": "string"},
            "method": {"type": "string"},
        },
        "required": ["url"],
    },
    "agent-health": AGENT_HEALTH_INPUT_SCHEMA,
    "montage": MONTAGE_INPUT_SCHEMA,
    "decide": DECIDE_INPUT_SCHEMA,
    "guard": GUARD_INPUT_SCHEMA,
    "verify": VERIFY_INPUT_SCHEMA,
    "rank": RANK_INPUT_SCHEMA,
}

_OUTPUT_SCHEMAS = {
    "discover": DISCOVER_OUTPUT_SCHEMA,
    "search": SEARCH_OUTPUT_SCHEMA,
    "translate": TRANSLATE_OUTPUT_SCHEMA,
    "jobs": JOBS_OUTPUT_SCHEMA,
    "pdf": PDF_OUTPUT_SCHEMA,
    "web-read": WEB_READ_OUTPUT_SCHEMA,
    "extract": EXTRACT_OUTPUT_SCHEMA,
    "summarize": SUMMARIZE_OUTPUT_SCHEMA,
    "fact-check": FACT_CHECK_OUTPUT_SCHEMA,
    "weather": WEATHER_OUTPUT_SCHEMA,
    "crypto": CRYPTO_OUTPUT_SCHEMA,
    "news": NEWS_OUTPUT_SCHEMA,
    "can-pay": CAN_PAY_OUTPUT_SCHEMA,
    "probe": PROBE_OUTPUT_SCHEMA,
    "wallet-balance": WALLET_BALANCE_OUTPUT_SCHEMA,
    "gas-price": GAS_PRICE_OUTPUT_SCHEMA,
    "v1/chat/completions": LLM_GATEWAY_OUTPUT_SCHEMA,
    "llm/claude-sonnet": LLM_GATEWAY_OUTPUT_SCHEMA,
    "llm/gpt-mini": LLM_GATEWAY_OUTPUT_SCHEMA,
    "llm/llama": LLM_GATEWAY_OUTPUT_SCHEMA,
    "llm/gemini-flash": LLM_GATEWAY_OUTPUT_SCHEMA,
    "llm/deepseek": LLM_GATEWAY_OUTPUT_SCHEMA,
    "token-risk": TOKEN_RISK_OUTPUT_SCHEMA,
    "research": RESEARCH_OUTPUT_SCHEMA,
    "sentiment": SENTIMENT_OUTPUT_SCHEMA,
    "classify": CLASSIFY_OUTPUT_SCHEMA,
    "intent": INTENT_OUTPUT_SCHEMA,
    "spam-check": SPAM_CHECK_OUTPUT_SCHEMA,
    "toxicity": TOXICITY_OUTPUT_SCHEMA,
    "language": LANGUAGE_OUTPUT_SCHEMA,
    "pii-check": PII_CHECK_OUTPUT_SCHEMA,
    "wallet-intelligence": WALLET_INTELLIGENCE_OUTPUT_SCHEMA,
    "x402-echo": X402_ECHO_OUTPUT_SCHEMA,
    "tip": X402_ECHO_OUTPUT_SCHEMA,
    "agent-claim": AGENT_HEALTH_OUTPUT_SCHEMA,
    "agent-health": AGENT_HEALTH_OUTPUT_SCHEMA,
    "montage": MONTAGE_OUTPUT_SCHEMA,
    "decide": DECIDE_OUTPUT_SCHEMA,
    "guard": GUARD_OUTPUT_SCHEMA,
    "verify": VERIFY_OUTPUT_SCHEMA,
    "rank": RANK_OUTPUT_SCHEMA,
}

X_GUIDANCE = (
    "AgentIndex x402 — USDC on Base, no account. "
    "MAINNET CLIENT TEST: GET /x402-echo?message=hello ($0.000001, one atomic USDC). "
    "CHEAPEST USEFUL CALLS: /search, /wallet-balance and /gas-price ($0.0001). "
    "Also $0.001 GET: /probe?url=... /weather?city=Paris /crypto?coins=btc "
    "/news?limit=10 ; POST /discover. Free .../sample. "
    "GET /agent.json /openapi.json /.well-known/x402 /llms.txt /mcp/."
)


def _price_to_amount_string(price: str) -> str:
    return f"{float(price.replace('$', '')):.6f}"


def _accept_example(payment_option) -> dict:
    asset = get_default_asset(payment_option.network)
    price_units = _price_to_amount_string(payment_option.price)
    amount_atomic = str(round(float(price_units) * (10 ** asset["decimals"])))
    return {
        "scheme": payment_option.scheme,
        "network": payment_option.network,
        "asset": asset["asset"],
        "amount": amount_atomic,
        "payTo": payment_option.pay_to,
        "extra": {"name": asset["name"], "version": asset["version"]},
    }


def _payment_response(payment_option) -> dict:
    return {
        "description": (
            "Payment required. Body carries an x402 PaymentRequired envelope "
            "(also base64-encoded in the payment-required response header); "
            "retry with a signed payment in the Payment-Signature request header."
        ),
        "headers": {
            "payment-required": {
                "description": "Base64-encoded x402 PaymentRequired challenge (JSON).",
                "schema": {"type": "string"},
            }
        },
        "content": {
            "application/json": {
                "example": {
                    "x402Version": 2,
                    "error": "Payment required",
                    "accepts": [_accept_example(payment_option)],
                }
            }
        },
    }


def _x_payment_info(payment_option, dynamic: bool = False) -> dict:
    protocols = [{"x402": {"version": 2}}]
    if config.CDP_WALLET_SECRET:
        protocols.append(
            {
                "mpp": {
                    "method": "evm",
                    "intent": "charge",
                    "currency": get_default_asset(payment_option.network)["asset"],
                }
            }
        )
    # Dynamic-price routes (currently only POST /v1/chat/completions, see
    # app/handlers/llm_gateway.py::compute_ceiling_price) have no single
    # real price - x402scan's discovery spec has a dedicated "dynamic"
    # price mode for exactly this (DISCOVERY.md, OpenAPI-first section).
    # Only MIN_SETTLE_USD is quoted: it's a real, already-public floor the
    # runtime itself enforces (never below it), not a fabricated ceiling -
    # a wrong "max" would be a worse inaccuracy than an absent one.
    price_block = (
        {"mode": "dynamic", "currency": "USD", "min": f"{MIN_SETTLE_USD:.6f}"}
        if dynamic
        else {
            # AgentCash's x-payment-info.price.currency is decimal-USD pricing
            # metadata (ISO 4217, so a strict 3-letter code) - a different field
            # from the runtime x402 challenge's on-chain asset (USDC on Base,
            # unaffected by this). Using "USDC" here fails their currency regex,
            # which silently discards this whole price+protocols block during
            # their validator's schema parse - not just a cosmetic mismatch.
            "mode": "fixed",
            "currency": "USD",
            "amount": _price_to_amount_string(payment_option.price),
        }
    )
    return {"price": price_block, "protocols": protocols}


def _request_body(input_schema: dict, example: dict | None = None) -> dict:
    json_body: dict = {"schema": {"type": "object", **input_schema}}
    if example is not None:
        json_body["example"] = example
    return {
        "required": True,
        "content": {"application/json": json_body},
    }


def _enrich_free_discover_operations(schema: dict) -> None:
    """GET /discover est hors build_route_configs ; les indexeurs lisent l'OpenAPI."""
    paths = schema.get("paths") or {}
    discover_get = (paths.get("/discover") or {}).get("get")
    if discover_get is not None:
        discover_get.setdefault("security", [])
        discover_get.setdefault("responses", {})
        discover_get["responses"]["200"] = {
            "description": (
                "Ranked MCP servers for q= (or a fixed example need when q is omitted, "
                "with a hint object)."
            ),
            "content": {
                "application/json": {
                    "schema": DISCOVER_OUTPUT_SCHEMA,
                    "example": DISCOVER_SAMPLE_OUTPUT,
                }
            },
        }
        for param in discover_get.get("parameters") or []:
            if param.get("name") in ("q", "query"):
                param.setdefault(
                    "example",
                    "persistent knowledge graph for AI agents",
                )
    sample_get = (paths.get("/discover/sample") or {}).get("get")
    if sample_get is not None:
        sample_get.setdefault("security", [])
        sample_get.setdefault("responses", {})
        sample_get["responses"]["200"] = {
            "description": "Same ranking as GET /discover with a fixed example need.",
            "content": {
                "application/json": {
                    "schema": DISCOVER_OUTPUT_SCHEMA,
                    "example": DISCOVER_SAMPLE_OUTPUT,
                }
            },
        }


def build_custom_openapi(app):
    def custom_openapi():
        if app.openapi_schema:
            return app.openapi_schema

        schema = get_openapi(
            title=app.title,
            version=app.version,
            description=app.description,
            routes=app.routes,
            contact=app.contact,
        )
        schema["info"]["x-guidance"] = X_GUIDANCE
        schema["externalDocs"] = {
            "description": "Agent-oriented capability card with pricing, schemas and live samples",
            "url": f"{config.BASE_URL}/agent.json",
        }
        # @agentcash/discovery's SPECIFICATION.md requires a `security` value
        # on every operation (`[]` for "no API auth" - x402 payment is not an
        # API key). Without it, discover()/check() flag the origin and their
        # indexer appears to drop it entirely (see BRIEF-CORRECTIONS.md /
        # the Agent Guild openapi.json diff for the confirmed gap - every
        # operation there carries an explicit `security: []`, ours had none).
        schema["x-agentcash-guidance"] = {"llmsTxtUrl": f"{config.BASE_URL}/llms.txt"}

        # AgentCash's discovery spec (agentcash.dev/discovery) requires
        # x-discovery.ownershipProofs to consider a listing's origin
        # verified: an EIP-191 personal_sign of the bare origin URL (no
        # trailing slash, no path) by a private key controlling one of our
        # payTo addresses - AgentCash recovers the signer and matches it
        # against payTo. Signed once by hand (2026-09-29, A =
        # config.X402_PAY_TO) via scripts/sign_ownership_proof.py (run
        # locally, key never touched this codebase or any log). Emitted
        # under both keys on purpose: the documented/canonical name is
        # x-discovery, but at least one independent source describes
        # AgentCash's actual reader checking x-agentcash-provenance first -
        # a wrong key here fails silently (no error, just treated as
        # unverified), so there's no cost to covering both instead of
        # guessing which is authoritative today.
        ownership_proof = {
            "ownershipProofs": [
                "0x9f7bebd594a66719221fdb098fcd8ddf050f75306e3690aa1a05376c1be7c6e37d0c9005024a6e767c59d05bba0db286ae3c92260b7aae3a76481e793c45f32e1c"
            ]
        }
        schema["x-discovery"] = ownership_proof
        schema["x-agentcash-provenance"] = ownership_proof

        generated_specs = {spec.slug: spec for spec in live_routes()}
        # purecalc's own ComputeSpec registry (see app/purecalc/registry.py)
        # is a third schema source, same priority tier as generated_specs -
        # a real, type-checked Pydantic model per route, never a hand-copied
        # dict that can drift from app/purecalc/routes/*.py (the 2026-10-03
        # agentcash-discovery audit found every one of these ~65 routes
        # missing x-payment-info for exactly that reason: no entry existed
        # in _INPUT_SCHEMAS/_OUTPUT_SCHEMAS and nobody had hand-added one).
        purecalc_specs = {spec.slug: spec for spec in COMPUTE_SPECS}
        base_rpc_specs = {spec.slug: spec for spec in BASE_RPC_SPECS}

        for route_key, route_config in build_route_configs().items():
            method, path = route_key.split(" ", 1)
            operation = ((schema.get("paths") or {}).get(path) or {}).get(method.lower())
            if operation is None:
                continue
            route_name = path.lstrip("/")
            generated = generated_specs.get(route_name)
            purecalc_spec = purecalc_specs.get(route_name)
            base_rpc_spec = base_rpc_specs.get(route_name)
            payment_option = route_config.accepts
            if isinstance(payment_option, list):
                payment_option = payment_option[0]
            # POST /v1/chat/completions' accepts.price is a DynamicPrice
            # callable (see app/handlers/llm_gateway.py::compute_ceiling_price),
            # not a dollar-string - build a stand-in with the same scheme/
            # network/pay_to for the illustrative 402 example body, and tell
            # _x_payment_info to emit a "dynamic" price block instead of a
            # fixed amount it can't compute statically.
            price_is_dynamic = callable(payment_option.price)
            example_payment_option = payment_option
            if price_is_dynamic:
                from types import SimpleNamespace

                example_payment_option = SimpleNamespace(
                    scheme=payment_option.scheme,
                    network=payment_option.network,
                    pay_to=payment_option.pay_to,
                    price=f"${MIN_SETTLE_USD:.6f}",
                )
            if generated:
                input_schema = generated.input_schema
                output_schema = generated.output_schema
            elif purecalc_spec:
                input_schema = inline_json_schema(purecalc_spec.input_model)
                output_schema = inline_json_schema(purecalc_spec.output_model)
            elif base_rpc_spec:
                input_schema = inline_json_schema(base_rpc_spec.input_model)
                output_schema = inline_json_schema(base_rpc_spec.output_model)
            else:
                input_schema = _INPUT_SCHEMAS.get(route_name)
                output_schema = _OUTPUT_SCHEMAS.get(route_name)
            if not input_schema or not output_schema:
                continue
            operation["x-payment-info"] = _x_payment_info(example_payment_option, dynamic=price_is_dynamic)
            operation.setdefault("security", [])
            operation.setdefault("responses", {})["402"] = _payment_response(example_payment_option)
            input_example = DISCOVER_INPUT_EXAMPLE if route_name == "discover" else None
            operation["requestBody"] = _request_body(input_schema, example=input_example)
            # Discoverability material for AgentCash's semantic search (see
            # BRIEF-CORRECTIONS.md): their indexer synthesizes ranking
            # keywords/use-cases from summary + description + schemas, not
            # from a dedicated field - confirmed by inspecting a ranked
            # competitor's openapi.json, which has neither `tags` nor a
            # use-cases field. `tags`/`x-use-cases` are still set (standard
            # OpenAPI field, and a low-cost hedge respectively) but the real
            # lever is the fully-described request/response schemas above.
            # Usine-generated routes (see app/generated/) carry their own
            # summary/use-cases/output_schema on the RouteSpec itself, set by
            # Ouvrier when the route was created - same treatment, different
            # source, never a third copy of this logic.
            if generated:
                operation["summary"] = generated.summary
                operation["x-use-cases"] = generated.use_cases
            elif purecalc_spec:
                operation["summary"] = purecalc_spec.service_name
                operation["x-use-cases"] = []
            elif base_rpc_spec:
                operation["summary"] = base_rpc_spec.service_name
                operation["x-use-cases"] = []
            else:
                operation["summary"] = ROUTE_SUMMARIES.get(route_name, route_name)
                operation["x-use-cases"] = ROUTE_USE_CASES.get(route_name, [])
            operation["tags"] = route_config.tags
            success_json: dict = {"schema": output_schema}
            if route_name == "discover":
                success_json["example"] = DISCOVER_SAMPLE_OUTPUT
            operation["responses"]["200"] = {
                "description": "Successful Response",
                "content": {"application/json": success_json},
            }

        # /token-risk, /research, the six sentiment/classify/intent/
        # spam-check/toxicity/language/pii-check routes, and the per-model
        # llm/* shortcuts all had the same gap as /v1/chat/completions: a
        # real schema already existed (reused by their own
        # declare_discovery_extension call) but no entry in
        # _INPUT_SCHEMAS/_OUTPUT_SCHEMAS, so the loop above skipped them
        # and this file used to patch just security: [] in here instead -
        # agentcash discover flagged exactly this set as L2/L3_AUTH_MODE_
        # MISSING or outright "unprotected" (2026-09-29 / 2026-10-03). All
        # now wired into _INPUT_SCHEMAS/_OUTPUT_SCHEMAS above, reusing each
        # route's existing constant - nothing left needing this fallback.

        _enrich_free_discover_operations(schema)

        # This document is a paid-service sales surface, not an application
        # debug dump. FastAPI otherwise exposes OAuth helpers, health probes,
        # samples and manifests as 80+ operations; discovery clients warn,
        # spend tokens on irrelevant routes and dilute semantic ranking.
        # Keep only routes an agent can actually buy. Free samples and
        # manifests remain linked from descriptions, llms.txt and agent.json.
        paid_paths = {
            route_key.split(" ", 1)[1] for route_key in build_route_configs()
        }
        schema["paths"] = {
            path: value for path, value in schema["paths"].items()
            if path in paid_paths
        }

        app.openapi_schema = schema
        return app.openapi_schema

    return custom_openapi
