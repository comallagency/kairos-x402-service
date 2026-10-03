"""base/basename - Basenames (ENS-equivalent on Base) forward resolution,
name -> address. See app/base_chain/registry.py.

Contract addresses verified on-chain (2026-10-03), not trusted from
documentation alone - the L2 Registry's resolver(node) for a real registered
name (jesse.base.eth) returns exactly this L2Resolver address, and
resolver.addr(node) cleanly returns 0x0 (not a revert) for an unregistered
name, so a direct call to the known default resolver is a single round
trip without needing the registry hop first:
  L2 Registry:  0xb94704422c2a1e396835a571837aa5ae53285a95
  L2 Resolver:  0xC6d566A56A1aFf6508b41f6c90ff131615583BCD
Reverse resolution (address -> name) is out of scope here: the ReverseRegistrar
(0x79ea96...9e282) does not expose it through the same registry.resolver()
path used for forward lookups (verified: the reverse node's resolver() comes
back zero even for a name with a real forward record), and getting it right
would need more on-chain research than this pack's scope justifies for one
route - forward resolution is the primary, unambiguous "ENS-equivalent"
lookup agents ask for.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, field_validator

from app.base_chain.registry import BaseRpcSpec, RpcComputeError, register
from app.base_chain.rpc_client import decode_address, multicall, selector

L2_RESOLVER = "0xC6d566A56A1aFf6508b41f6c90ff131615583BCD"
_ADDR_SEL = selector("addr(bytes32)")
_NAME_RE = re.compile(r"^[a-z0-9-]+\.base\.eth$")


def _namehash(name: str) -> bytes:
    """ENS namehash (EIP-137): standard across every .eth/.base.eth name,
    independent of which registry/resolver ends up answering for it."""
    from eth_utils import keccak

    node = b"\x00" * 32
    for label in reversed(name.split(".")):
        node = keccak(node + keccak(text=label))
    return node


class BasenameInput(BaseModel):
    name: str

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v):
        v = v.strip().lower()
        if not _NAME_RE.match(v):
            raise ValueError("name must look like 'label.base.eth'")
        return v


class BasenameOutput(BaseModel):
    name: str
    address: str


async def compute_basename(inp: BasenameInput) -> BasenameOutput:
    node = _namehash(inp.name)
    results = await multicall([(L2_RESOLVER, True, _ADDR_SEL + node)])
    ok, data = results[0]
    if not ok:
        raise RpcComputeError("resolver_call_failed", f"addr(bytes32) reverted for {inp.name}")
    address = decode_address(data)
    if address is None:
        raise RpcComputeError("name_not_found", f"{inp.name} is not registered or has no address record")
    return BasenameOutput(name=inp.name, address=address)


register(BaseRpcSpec(
    slug="base/basename", price="$0.005", service_name="base-basename",
    description="Resolve a Basename (Base's native ENS-equivalent, e.g. 'jesse.base.eth') to its wallet address via the L2 Basenames resolver.",
    tags=["basename", "base rpc", "ens base", "name resolution", "address lookup", "base mainnet"],
    input_model=BasenameInput, output_model=BasenameOutput, compute=compute_basename,
    sample_input={"name": "jesse.base.eth"},
    sample_output={"name": "jesse.base.eth", "address": "0x2211d1d0020daea8039e46cf1367962070d77da9"},
))
