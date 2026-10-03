"""base/nonce, base/code - simple per-address reads. See
app/base_chain/registry.py."""

from __future__ import annotations

from pydantic import BaseModel, field_validator

from app.base_chain.registry import BaseRpcSpec, register
from app.base_chain.rpc_client import call, is_valid_address


class AddressInput(BaseModel):
    address: str

    @field_validator("address")
    @classmethod
    def _validate_address(cls, v):
        if not is_valid_address(v):
            raise ValueError("address must be a 0x-prefixed 20-byte hex string")
        return v


# -------------------------------------------------------------------- base/nonce
class NonceOutput(BaseModel):
    address: str
    nonce: int


async def compute_nonce(inp: AddressInput) -> NonceOutput:
    hex_result = await call("eth_getTransactionCount", [inp.address, "latest"])
    return NonceOutput(address=inp.address, nonce=int(hex_result, 16))


register(BaseRpcSpec(
    slug="base/nonce", price="$0.003", service_name="base-nonce",
    description="Next transaction nonce for any Base mainnet wallet via eth_getTransactionCount - how many transactions it has sent.",
    tags=["base rpc", "nonce", "wallet", "base mainnet", "eth_getTransactionCount"],
    input_model=AddressInput, output_model=NonceOutput, compute=compute_nonce,
    sample_input={"address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"},
    sample_output={"address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "nonce": 1},
))


# --------------------------------------------------------------------- base/code
class CodeOutput(BaseModel):
    address: str
    is_contract: bool
    bytecode_size: int


async def compute_code(inp: AddressInput) -> CodeOutput:
    code = await call("eth_getCode", [inp.address, "latest"])
    if not isinstance(code, str) or code in ("0x", "0x0"):
        return CodeOutput(address=inp.address, is_contract=False, bytecode_size=0)
    return CodeOutput(address=inp.address, is_contract=True, bytecode_size=max(len(code) // 2 - 1, 0))


register(BaseRpcSpec(
    slug="base/code", price="$0.003", service_name="base-code",
    description="Check whether a Base mainnet address is a contract via eth_getCode - returns bytecode size, zero for an externally-owned account.",
    tags=["base rpc", "is contract", "bytecode", "base mainnet", "eth_getCode"],
    input_model=AddressInput, output_model=CodeOutput, compute=compute_code,
    sample_input={"address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"},
    sample_output={"address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "is_contract": True, "bytecode_size": 1852},
))
