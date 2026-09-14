"""Builds the FastAPI routers, x402 RouteConfigs and route-key mappings for
every live entry in the registry (app/generated/registry.py). Everything
here is derived from the registry at call time - never a hand-copied list -
so a route Fossoyeur retires (status: retired) or Ouvrier adds disappears or
appears the next time the app starts, with zero other code changes needed.

Consumed by:
- app/x402_setup.py::build_route_configs() - merges these into the 3 core
  RouteConfigs so the x402 payment challenge, well-known, and MCP discovery
  extensions treat generated routes identically to hand-built ones.
- app/capacity.py::ROUTE_KEYS - so the capacity gate, intent logging and MPP
  middleware all recognize generated routes too.
- app/main.py - includes the generated APIRouters.
"""

from x402.extensions.bazaar import OutputConfig, declare_discovery_extension
from x402.http.types import PaymentOption, RouteConfig

from app import config
from app.generated.proxy_handler import build_proxy_router
from app.generated.registry import RouteSpec, live_routes


def build_dynamic_routers() -> list:
    return [build_proxy_router(spec) for spec in live_routes() if spec.handler_type == "http_proxy"]


def _route_config_for(spec: RouteSpec) -> RouteConfig:
    return RouteConfig(
        accepts=PaymentOption(
            scheme="exact", pay_to=config.X402_PAY_TO, price=spec.price, network=config.X402_NETWORK
        ),
        resource=f"{config.BASE_URL}/{spec.slug}",
        description=spec.description,
        mime_type="application/json",
        service_name=spec.slug,
        tags=spec.tags,
        extensions=declare_discovery_extension(
            input_schema=spec.input_schema,
            body_type="json",
            output=OutputConfig(schema=spec.output_schema),
        ),
    )


def build_dynamic_route_configs() -> dict[str, RouteConfig]:
    return {f"POST /{spec.slug}": _route_config_for(spec) for spec in live_routes()}


def dynamic_route_keys() -> dict[tuple[str, str], str]:
    return {("POST", f"/{spec.slug}"): spec.slug for spec in live_routes()}


def dynamic_daily_capacity() -> dict[str, int]:
    return {spec.slug: spec.daily_capacity for spec in live_routes()}
