"""Generic FastAPI + x402 + MCP wiring for every ComputeSpec in
app.purecalc.registry.COMPUTE_SPECS. See registry.py's module docstring -
this is the one place that plumbing lives.

Error contract (point 1 of the 2026-10-02 pure-compute pack): a schema
violation (Pydantic ValidationError) or a semantic one (ComputeError, e.g.
a bad checksum) both become 422, free (never logged as paid, never
charged). Anything else unexpected from compute() is also caught and
turned into a 422 with the real exception logged server-side via
error_reason - a genuine bug should show up in route_checks/journal, not
as a 500 a buyer's client has to handle specially. This is a deliberate,
narrow safety net around exactly one call (spec.compute(parsed)), not a
general catch-all - the "jamais 5xx" requirement is explicit and testable
per-route by the 5 reference cases, this is defense in depth beyond that.
"""

import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from x402.extensions.bazaar import OutputConfig, declare_discovery_extension
from x402.http.types import PaymentOption, RouteConfig

from app import config, db
from app.purecalc.registry import ComputeError, ComputeSpec, COMPUTE_SPECS, inline_json_schema
from app.receipts import Timer, effective_price, extract_payer_address, make_receipt, price_float

UPSTREAM_KIND = "pure_compute"


def _validation_detail(exc: ValidationError) -> list[dict]:
    return [
        {"field": ".".join(str(p) for p in e["loc"]), "reason": e["type"], "message": e["msg"]}
        for e in exc.errors()
    ]


def build_compute_router(spec: ComputeSpec) -> APIRouter:
    router = APIRouter()
    path = f"/{spec.slug}"

    @router.get(f"{path}/sample", openapi_extra={"security": []}, name=f"{spec.slug}_sample")
    async def sample():
        return {**spec.sample_output, "x402_receipt": make_receipt(None, UPSTREAM_KIND, 1, 0.0)}

    @router.post(path, description=spec.description, name=spec.slug)
    async def handler(request: Request):
        payer = extract_payer_address(request)
        user_agent = request.headers.get("user-agent")
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
                result = spec.compute(parsed)
        except ComputeError as exc:
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
        return {**result.model_dump(), "x402_receipt": receipt}

    return router


def build_compute_routers() -> list[APIRouter]:
    return [build_compute_router(spec) for spec in COMPUTE_SPECS]


def _route_config_for(spec: ComputeSpec) -> RouteConfig:
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


def build_compute_route_configs() -> dict[str, RouteConfig]:
    configs = {}
    for spec in COMPUTE_SPECS:
        cfg = _route_config_for(spec)
        configs[f"POST /{spec.slug}"] = cfg
        # GET twin from day one (2026-10-01 fix, applied here pre-emptively
        # instead of retrofitted): an unpaid GET probe must get 402, never
        # FastAPI's own 405 for a method with no registered handler.
        configs[f"GET /{spec.slug}"] = cfg
    return configs
