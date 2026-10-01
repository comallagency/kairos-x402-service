"""Single registry for the pure-compute pack (2026-10-02): 51 routes, zero
external dependency at request time, < 50ms target, hand-authored (never
touched by the usine/Fossoyeur lifecycle in app/generated/ - that system
is for autonomously generated upstream-proxy routes, not permanent
hand-built compute logic).

Each route file under app/purecalc/routes/ defines an input Pydantic
model, an output Pydantic model, a synchronous compute(input) -> output
function, and a `register(...)` call - app/purecalc/engine.py is the ONLY
place that turns a ComputeSpec into a FastAPI router, an x402 RouteConfig
pair (GET+POST, the probe-friendliness fix from 2026-10-01 built in from
the start this time), and an MCP tool. Never copy that plumbing per route.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Type

from pydantic import BaseModel


class ComputeError(Exception):
    """Raised by a compute() function for a schema-valid but semantically
    invalid input (e.g. a checksum that doesn't add up) - always becomes a
    422, never a 5xx, same guarantee as a Pydantic ValidationError."""

    def __init__(self, reason: str, detail: str | None = None):
        self.reason = reason
        self.detail = detail
        super().__init__(reason)


@dataclass
class ComputeSpec:
    slug: str  # "geo/distance" -> POST/GET /geo/distance
    price: str  # "$0.002"
    service_name: str
    description: str
    tags: list[str]
    input_model: Type[BaseModel]
    output_model: Type[BaseModel]
    compute: Callable[[BaseModel], BaseModel]
    sample_input: dict
    sample_output: dict  # static, precomputed by hand - never from a live compute() call

    @property
    def mcp_tool_name(self) -> str:
        """MCP tool names may not contain '/' (SEP-986) - the HTTP slug
        does and must (/geo/distance), so this is a distinct derived name,
        never the other way around."""
        return self.slug.replace("/", "_")


def inline_json_schema(model: Type[BaseModel]) -> dict:
    """model.model_json_schema() puts a nested submodel (e.g. LatLon reused
    by point-in-polygon and bbox) under top-level $defs with a $ref - fine
    on its own, but x402's declare_discovery_extension/declare_mcp_
    discovery_extension embed this schema under a `body`/`payload` key, so
    the $ref (always `#/$defs/X`, resolved from the schema ROOT) no longer
    points at anything once nested - a real, reproducible failure (not
    hypothetical: POST/GET /geo/point-in-polygon and /geo/bbox both failed
    their bazaar extension schema validation with this exact shape before
    this existed). Recursively inlines every $ref so the result is fully
    self-contained, independent of where it ends up nested."""
    import copy

    schema = model.model_json_schema()
    defs = schema.pop("$defs", {})
    if not defs:
        return schema

    def resolve(node):
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                target = copy.deepcopy(defs[ref.rsplit("/", 1)[-1]])
                target.update({k: v for k, v in node.items() if k != "$ref"})
                return resolve(target)
            return {k: resolve(v) for k, v in node.items()}
        if isinstance(node, list):
            return [resolve(v) for v in node]
        return node

    return resolve(schema)


COMPUTE_SPECS: list[ComputeSpec] = []
_SEEN_SLUGS: set[str] = set()


def register(spec: ComputeSpec) -> ComputeSpec:
    if spec.slug in _SEEN_SLUGS:
        raise ValueError(f"duplicate purecalc slug: {spec.slug}")
    _SEEN_SLUGS.add(spec.slug)
    COMPUTE_SPECS.append(spec)
    return spec
