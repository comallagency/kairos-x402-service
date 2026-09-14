"""Single source of truth for routes the usine (Prospecteur/Ouvrier/Crieur,
see usine/) has generated - a plain YAML file, not a database, so it's
reviewable in a diff and deployed the same way as the rest of the app code
(tar + scp + docker compose build). Fossoyeur (scripts/fossoyeur.py, runs on
the VPS host) edits this file directly and triggers a rebuild to retire a
dead route - it never touches app/x402_setup.py's hand-built 3 core routes.

Everything downstream (app/x402_setup.py::build_route_configs(),
app/capacity.py::ROUTE_KEYS, app/openapi_custom.py, app/mcp_server.py) reads
this registry fresh - never a hand-copied list, matching every other
single-source-of-truth in this project.
"""

from dataclasses import asdict, dataclass
from pathlib import Path

import yaml

REGISTRY_PATH = Path(__file__).parent / "routes_registry.yaml"


@dataclass
class RouteSpec:
    slug: str  # URL path segment: POST /<slug>
    intention: str  # what the buyer is trying to do - internal, for the dashboard/journal
    price: str  # "$0.05" - same format as config.PRICE_SEARCH etc.
    summary: str  # OpenAPI `summary`, one sentence
    description: str  # buyer-facing description, never names the upstream vendor
    tags: list[str]  # 5-8 keywords, same convention as the 3 core routes
    use_cases: list[str]  # 8 imperative sentences
    input_schema: dict  # JSON Schema, same shape as SEARCH_INPUT_SCHEMA etc.
    output_schema: dict  # JSON Schema for the 200 response
    upstream: dict  # {"url": str, "method": "GET"|"POST"} - never exposed publicly
    born_at: str  # ISO date, used by Fossoyeur's 30-day rule
    handler_type: str = "http_proxy"  # only generic type implemented so far
    daily_capacity: int = 100  # same purpose as config.DAILY_CAPACITY for the core routes
    status: str = "live"  # "live" | "retired"

    def __post_init__(self):
        if not self.slug or "/" in self.slug:
            raise ValueError(f"invalid slug: {self.slug!r}")
        if self.handler_type != "http_proxy":
            raise ValueError(f"unsupported handler_type: {self.handler_type!r}")


def load_registry() -> list[RouteSpec]:
    if not REGISTRY_PATH.exists():
        return []
    data = yaml.safe_load(REGISTRY_PATH.read_text(encoding="utf-8")) or {}
    return [RouteSpec(**entry) for entry in data.get("routes", [])]


def save_registry(specs: list[RouteSpec]) -> None:
    data = {"routes": [asdict(spec) for spec in specs]}
    REGISTRY_PATH.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100), encoding="utf-8"
    )


def live_routes() -> list[RouteSpec]:
    return [spec for spec in load_registry() if spec.status == "live"]


def get_route(slug: str) -> RouteSpec | None:
    for spec in load_registry():
        if spec.slug == slug:
            return spec
    return None
