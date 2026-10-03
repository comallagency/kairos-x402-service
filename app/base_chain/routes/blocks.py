"""base/block, base/pending - block headers by number/tag/hash, and the
current pending block. See app/base_chain/registry.py."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, field_validator

from app.base_chain.registry import BaseRpcSpec, RpcComputeError, register
from app.base_chain.rpc_client import call, is_valid_hash

_TAGS = {"latest", "earliest", "pending", "safe", "finalized"}


def _normalize_block_number(value) -> str:
    if isinstance(value, str) and value.lower() in _TAGS:
        return value.lower()
    if isinstance(value, str) and value.startswith("0x"):
        try:
            int(value, 16)
            return value
        except ValueError:
            raise RpcComputeError("invalid_block_number", f"not a valid hex block tag: {value!r}")
    if isinstance(value, str) and value.isdigit():
        value = int(value)
    if isinstance(value, int):
        if value < 0:
            raise RpcComputeError("invalid_block_number", "block number must be >= 0")
        return hex(value)
    raise RpcComputeError("invalid_block_number", f"unrecognized block number/tag: {value!r}")


def _block_output(raw: dict) -> "BlockOutput":
    return BlockOutput(
        number=int(raw["number"], 16),
        hash=raw["hash"],
        parent_hash=raw["parentHash"],
        timestamp=int(raw["timestamp"], 16),
        miner=raw["miner"],
        gas_used=int(raw["gasUsed"], 16),
        gas_limit=int(raw["gasLimit"], 16),
        base_fee_per_gas=int(raw["baseFeePerGas"], 16) if raw.get("baseFeePerGas") else None,
        transaction_count=len(raw.get("transactions") or []),
        transaction_hashes=list(raw.get("transactions") or []),
    )


class BlockOutput(BaseModel):
    number: int
    hash: str
    parent_hash: str
    timestamp: int
    miner: str
    gas_used: int
    gas_limit: int
    base_fee_per_gas: Optional[int] = None
    transaction_count: int
    transaction_hashes: list[str]


# -------------------------------------------------------------------- base/block
class BlockInput(BaseModel):
    number: Optional[object] = None
    hash: Optional[str] = None

    @field_validator("hash")
    @classmethod
    def _validate_hash(cls, v):
        if v is not None and not is_valid_hash(v):
            raise ValueError("hash must be a 0x-prefixed 32-byte hex string")
        return v


async def compute_block(inp: BlockInput) -> BlockOutput:
    if inp.hash:
        raw = await call("eth_getBlockByHash", [inp.hash, False])
    else:
        tag = _normalize_block_number(inp.number if inp.number is not None else "latest")
        raw = await call("eth_getBlockByNumber", [tag, False])
    if raw is None:
        raise RpcComputeError("block_not_found", "no block matches that number/tag/hash")
    return _block_output(raw)


register(BaseRpcSpec(
    slug="base/block", price="$0.003", service_name="base-block",
    description="Fetch a Base mainnet block header and transaction hashes by number, tag (latest/earliest/safe/finalized), or hash via eth_getBlockByNumber/eth_getBlockByHash.",
    tags=["base rpc", "block header", "base mainnet", "eth_getBlockByNumber", "eth_getBlockByHash"],
    input_model=BlockInput, output_model=BlockOutput, compute=compute_block,
    sample_input={"number": 0},
    sample_output={
        "number": 0, "hash": "0xf712aa9241cc24369b143cf6dce85f0902a9731e70d66818a3a5845b296c73dd",
        "parent_hash": "0x0000000000000000000000000000000000000000000000000000000000000000",
        "timestamp": 1686789859, "miner": "0x4200000000000000000000000000000000000011",
        "gas_used": 0, "gas_limit": 30000000, "base_fee_per_gas": 1000000000,
        "transaction_count": 0, "transaction_hashes": [],
    },
))


# ------------------------------------------------------------------ base/pending
class PendingInput(BaseModel):
    pass


async def compute_pending(inp: PendingInput) -> BlockOutput:
    raw = await call("eth_getBlockByNumber", ["pending", False])
    if raw is None:
        raise RpcComputeError("pending_unavailable", "the RPC provider returned no pending block")
    return _block_output(raw)


register(BaseRpcSpec(
    slug="base/pending", price="$0.01", service_name="base-pending-block",
    description="The current pending (not-yet-mined) Base mainnet block via eth_getBlockByNumber('pending').",
    tags=["base rpc", "pending block", "mempool", "base mainnet", "unconfirmed transactions"],
    input_model=PendingInput, output_model=BlockOutput, compute=compute_pending,
    sample_input={},
    sample_output={
        "number": 33554433, "hash": "0x0000000000000000000000000000000000000000000000000000000000000000",
        "parent_hash": "0xf712aa9241cc24369b143cf6dce85f0902a9731e70d66818a3a5845b296c73dd",
        "timestamp": 1759500000, "miner": "0x4200000000000000000000000000000000000011",
        "gas_used": 0, "gas_limit": 30000000, "base_fee_per_gas": 10000000,
        "transaction_count": 0, "transaction_hashes": [],
    },
))
