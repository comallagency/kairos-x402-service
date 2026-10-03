"""Generic FastAPI + x402 + MCP wiring for every BaseRpcSpec in
app.base_chain.registry.BASE_RPC_SPECS. See registry.py's module docstring -
this is the one place that plumbing lives, mirroring
app/purecalc/engine.py's shape with the two differences required for an
I/O-bound (RPC-backed) pack instead of a pure-compute one:

1. compute() is awaited, and a timeout (app.base_chain.rpc_client.RpcTimeout,
   raised past the pack's 3s ceiling) becomes a free 504 - never settled,
   never logged as paid - instead of folding into the generic 422 safety
   net below it. A slow RPC provider is not the caller's fault and is not
   evidence of a bad request; charging for a response we didn't deliver
   in time would be exactly the "encaissement sans livraison" this pack
   must never do.
2. RpcComputeError (malformed address, unknown hash, etc.) is the
   schema-valid-but-semantically-invalid case, same contract as
   purecalc's ComputeError: always 422, free, never 5xx.

Error contract, otherwise identical to purecalc: a Pydantic ValidationError
or an RpcComputeError is 422 and free. Anything else unexpected from
compute() is also caught and turned into a 422 with the real exception
logged server-side via error_reason - a genuine bug should show up in
route_checks/journal, not as a 500 a buyer's client has to handle specially.

Payment verification: identical belt-and-suspenders check as purecalc
(request.state.payment_payload or current_mpp_verified_payer()) - this
handler does not trust PaymentMiddlewareASGI/MPPMiddleware blindly, for
the same reason documented in purecalc/engine.py (the 2026-10-02 incident).
"""

import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from x402.extensions.bazaar import OutputConfig, declare_discovery_extension
from x402.http.types import PaymentOption, RouteConfig

from app import config, db
from app.base_chain.registry import BaseRpcSpec, RpcComputeError, BASE_RPC_SPECS
from app.base_chain.rpc_client import RpcTimeout
from app.purecalc.registry import inline_json_schema
from app.receipts import Timer, effective_price, extract_payer_from_payment_dict, make_receipt, price_float

UPSTREAM_KIND = "base_rpc"


def _validation_detail(exc: ValidationError) -> list[dict]:
    return [
        {"field": ".".join(str(p) for p in e["loc"]), "reason": e["type"], "message": e["msg"]}
        for e in exc.errors()
    ]


def build_rpc_router(spec: BaseRpcSpec) -> APIRouter:
    router = APIRouter()
    path = f"/{spec.slug}"

    @router.get(f"{path}/sample", openapi_extra={"security": []}, name=f"{spec.slug}_sample")
    async def sample():
        return {**spec.sample_output, "x402_receipt": make_receipt(None, UPSTREAM_KIND, 1, 0.0)}

    @router.post(path, description=spec.description, name=spec.slug)
    async def handler(request: Request):
        from app.mpp_middleware import current_mpp_verified_payer

        user_agent = request.headers.get("user-agent")
        payment_payload = getattr(request.state, "payment_payload", None)
        if payment_payload is not None:
            payer = extract_payer_from_payment_dict(
                payment_payload.model_dump(by_alias=True, exclude_none=True)
            )
        else:
            payer = current_mpp_verified_payer()
        if not payer:
            db.log_request(
                route=spec.slug, method="POST", status="payment_failed",
                user_agent=user_agent, error_reason="no_verified_payment",
            )
            return JSONResponse(
                {"error": {"reason": "payment_required", "detail": "no verified payment for this request"}},
                status_code=402,
            )
        try:
            body = await request.json()
        except Exception:
            body = {}
        body_excerpt = json.dumps(body)[:2000]

        try:
            parsed = spec.input_model(**body)
        except ValidationError as exc:
            db.log_request(
                route=spec.slug, method="POST", status="error", payer=payer,
                user_agent=user_agent, body_excerpt=body_excerpt, error_reason="invalid_input",
            )
            return JSONResponse(
                {"error": {"reason": "invalid_input", "detail": _validation_detail(exc)}},
                status_code=422,
            )

        try:
            with Timer() as t:
                result = await spec.compute(parsed)
        except RpcTimeout as exc:
            db.log_request(
                route=spec.slug, method="POST", status="error", payer=payer,
                user_agent=user_agent, body_excerpt=body_excerpt, error_reason=str(exc),
            )
            return JSONResponse(
                {"error": {"reason": "upstream_timeout", "detail": "Base RPC did not answer in time; not charged."}},
                status_code=504,
            )
        except RpcComputeError as exc:
            db.log_request(
                route=spec.slug, method="POST", status="error", payer=payer,
                user_agent=user_agent, body_excerpt=body_excerpt, error_reason=exc.reason,
            )
            return JSONResponse(
                {"error": {"reason": exc.reason, "detail": exc.detail}}, status_code=422
            )
        except Exception as exc:
            db.log_request(
                route=spec.slug, method="POST", status="error", payer=payer,
                user_agent=user_agent, body_excerpt=body_excerpt, error_reason=str(exc)[:200],
            )
            return JSONResponse(
                {"error": {"reason": "compute_error", "detail": str(exc)[:200]}}, status_code=422
            )

        price = effective_price(payer, price_float(spec.price))
        db.log_request(
            route=spec.slug, method="POST", status="paid", latency_ms=t.elapsed_ms,
            amount_usdc=price, payer=payer, user_agent=user_agent, body_excerpt=body_excerpt,
        )
        receipt = make_receipt(None, UPSTREAM_KIND, t.elapsed_ms, price)
        return {**result.model_dump(by_alias=True), "x402_receipt": receipt}

    return router


def build_rpc_routers() -> list[APIRouter]:
    return [build_rpc_router(spec) for spec in BASE_RPC_SPECS]


def _route_config_for(spec: BaseRpcSpec) -> RouteConfig:
    return RouteConfig(
        accepts=PaymentOption(
            scheme="exact", pay_to=config.X402_PAY_TO, price=spec.price, network=config.X402_NETWORK
        ),
        resource=f"{config.BASE_URL}/{spec.slug}",
        description=spec.description,
        mime_type="application/json",
        service_name=spec.service_name,
        tags=spec.tags,
        extensions=declare_discovery_extension(
            input=spec.sample_input,
            input_schema=inline_json_schema(spec.input_model),
            body_type="json",
            output=OutputConfig(
                example=spec.sample_output, schema=inline_json_schema(spec.output_model)
            ),
        ),
    )


def build_rpc_route_configs() -> dict[str, RouteConfig]:
    configs = {}
    for spec in BASE_RPC_SPECS:
        cfg = _route_config_for(spec)
        configs[f"POST /{spec.slug}"] = cfg
        # GET twin so an unpaid GET probe gets 402, never FastAPI's own 405
        # for a method with no registered handler - same reasoning as
        # purecalc/engine.py's build_compute_route_configs().
        configs[f"GET /{spec.slug}"] = cfg
    return configs
