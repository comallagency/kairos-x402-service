"""base/storage, base/call - raw low-level RPC reads. See
app/base_chain/registry.py.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, field_validator

from app.base_chain.registry import BaseRpcSpec, RpcComputeError, register
from app.base_chain.rpc_client import RpcApplicationError, call, is_valid_address


def _check_address(v: str) -> str:
    if not is_valid_address(v):
        raise ValueError("must be a 0x-prefixed 20-byte hex string")
    return v


def _check_slot(v) -> str:
    if isinstance(v, int):
        if v < 0:
            raise ValueError("slot must be >= 0")
        return hex(v)
    if isinstance(v, str) and v.startswith("0x"):
        try:
            int(v, 16)
            return v
        except ValueError:
            raise ValueError("slot must be a valid hex string")
    raise ValueError("slot must be an int or a 0x-prefixed hex string")


# ------------------------------------------------------------------- base/storage
class StorageInput(BaseModel):
    address: str
    slot: object

    @field_validator("address")
    @classmethod
    def _validate_address(cls, v):
        return _check_address(v)

    @field_validator("slot")
    @classmethod
    def _validate_slot(cls, v):
        return _check_slot(v)


class StorageOutput(BaseModel):
    address: str
    slot: str
    value: str
    value_decimal: str


async def compute_storage(inp: StorageInput) -> StorageOutput:
    raw = await call("eth_getStorageAt", [inp.address, inp.slot, "latest"])
    return StorageOutput(address=inp.address, slot=inp.slot, value=raw, value_decimal=str(int(raw, 16)))


register(BaseRpcSpec(
    slug="base/storage", price="$0.005", service_name="base-storage",
    description="Read a contract storage slot on Base mainnet via eth_getStorageAt - the raw 32-byte value, decoded as hex and unsigned integer, zero-filled if unset.",
    tags=["base rpc", "storage slot", "eth_getStorageAt", "base mainnet", "raw contract state"],
    input_model=StorageInput, output_model=StorageOutput, compute=compute_storage,
    sample_input={"address": "0xb125e6687d4313864e53df431d5425969c15eb2f", "slot": "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"},
    sample_output={
        "address": "0xb125e6687d4313864e53df431d5425969c15eb2f",
        "slot": "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc",
        "value": "0x000000000000000000000000c1455ae6df6cd808ed677f048e434e22892682a7",
        "value_decimal": "1103243528667961629076029746875274476749108994727",
    },
))


# ---------------------------------------------------------------------- base/call
class CallInput(BaseModel):
    to: str
    data: str = "0x"
    from_address: Optional[str] = None
    value_wei: int = 0

    @field_validator("to", "from_address")
    @classmethod
    def _validate_address(cls, v):
        if v is not None and not is_valid_address(v):
            raise ValueError("must be a 0x-prefixed 20-byte hex string")
        return v

    @field_validator("data")
    @classmethod
    def _validate_data(cls, v):
        if not v.startswith("0x") or len(v) % 2 != 0:
            raise ValueError("data must be a 0x-prefixed, even-length hex string")
        try:
            int(v, 16) if v != "0x" else None
        except ValueError:
            raise ValueError("data must be valid hex")
        return v

    @field_validator("value_wei")
    @classmethod
    def _validate_value(cls, v):
        if v < 0:
            raise ValueError("value_wei must be >= 0")
        return v


class CallOutput(BaseModel):
    to: str
    return_data: str


async def compute_call(inp: CallInput) -> CallOutput:
    params = {"to": inp.to, "data": inp.data}
    if inp.from_address:
        params["from"] = inp.from_address
    if inp.value_wei:
        params["value"] = hex(inp.value_wei)
    try:
        result = await call("eth_call", [params, "latest"])
    except RpcApplicationError as exc:
        raise RpcComputeError("call_reverted", str(exc.message))
    return CallOutput(to=inp.to, return_data=result)


register(BaseRpcSpec(
    slug="base/call", price="$0.005", service_name="base-call",
    description="Generic read-only eth_call against any Base mainnet contract - pass raw calldata, get the raw return data back.",
    tags=["base rpc", "eth_call", "generic call", "base mainnet", "contract read"],
    input_model=CallInput, output_model=CallOutput, compute=compute_call,
    sample_input={"to": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "data": "0x313ce567"},
    sample_output={"to": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "return_data": "0x0000000000000000000000000000000000000000000000000000000000000006"},
))
