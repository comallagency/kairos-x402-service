"""Single registry for the Base read-only RPC pack ("PACK LECTURE BASE",
2026-10-03): 25 routes reading Base mainnet state through public RPC only
(app/base_chain/rpc_client.py) - no paid upstream, no API key.

Same shape as app/purecalc/registry.py on purpose (one ComputeSpec-style
dataclass, one register() call per route file, app/base_chain/engine.py is
the only place that turns a spec into a FastAPI router, an x402 RouteConfig
pair, and an MCP tool) with one necessary difference: compute() here is a
coroutine function (RPC is I/O, purecalc's compute() never is), and a spec
carries no sample_output placeholder of its own invention - samples are
real captured responses (see each route file), never fabricated, per the
"un /sample statique reel" requirement.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable, Type

from pydantic import BaseModel


class RpcComputeError(Exception):
    """Raised by a compute() function for a schema-valid but semantically
    invalid input (malformed address, unknown/malformed hash) - always a
    free 422, never a 5xx. Distinct from RpcTimeout (app/base_chain/
    rpc_client.py), which is a free 504, not a 422: a timeout is not the
    caller's fault."""

    def __init__(self, reason: str, detail: str | None = None):
        self.reason = reason
        self.detail = detail
        super().__init__(reason)


@dataclass
class BaseRpcSpec:
    slug: str  # "base/erc20-balance" -> POST/GET /base/erc20-balance
    price: str  # "$0.003"
    service_name: str
    description: str
    tags: list[str]
    input_model: Type[BaseModel]
    output_model: Type[BaseModel]
    compute: Callable[[BaseModel], Awaitable[BaseModel]]
    sample_input: dict
    sample_output: dict  # a REAL captured response, never invented

    @property
    def mcp_tool_name(self) -> str:
        return self.slug.replace("/", "_")


BASE_RPC_SPECS: list[BaseRpcSpec] = []
_SEEN_SLUGS: set[str] = set()


def register(spec: BaseRpcSpec) -> BaseRpcSpec:
    if spec.slug in _SEEN_SLUGS:
        raise ValueError(f"duplicate base_chain slug: {spec.slug}")
    _SEEN_SLUGS.add(spec.slug)
    BASE_RPC_SPECS.append(spec)
    return spec
