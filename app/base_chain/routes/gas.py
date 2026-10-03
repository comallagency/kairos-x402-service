"""base/estimate-gas - eth_estimateGas for an arbitrary call. See
app/base_chain/registry.py.

A call that would revert (insufficient balance, a reverting contract call)
makes eth_estimateGas itself fail with a JSON-RPC error (EVM execution
reverted, code 3) - app.base_chain.rpc_client.call() raises
RpcApplicationError immediately for that (see its docstring), caught here
and turned into a free 422: the estimate itself is impossible for that
input, not an upstream problem.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, field_validator

from app.base_chain.registry import BaseRpcSpec, RpcComputeError, register
from app.base_chain.rpc_client import RpcApplicationError, call, is_valid_address


class EstimateGasInput(BaseModel):
    to: str
    from_address: Optional[str] = None
    value_wei: int = 0
    data: Optional[str] = None

    @field_validator("to", "from_address")
    @classmethod
    def _validate_address(cls, v):
        if v is not None and not is_valid_address(v):
            raise ValueError("must be a 0x-prefixed 20-byte hex string")
        return v

    @field_validator("value_wei")
    @classmethod
    def _validate_value(cls, v):
        if v < 0:
            raise ValueError("value_wei must be >= 0")
        return v

    @field_validator("data")
    @classmethod
    def _validate_data(cls, v):
        if v is not None:
            if not v.startswith("0x") or len(v) % 2 != 0:
                raise ValueError("data must be a 0x-prefixed, even-length hex string")
            try:
                int(v, 16) if v != "0x" else None
            except ValueError:
                raise ValueError("data must be valid hex")
        return v


class EstimateGasOutput(BaseModel):
    to: str
    from_address: Optional[str]
    value_wei: int
    gas_estimate: int


async def compute_estimate_gas(inp: EstimateGasInput) -> EstimateGasOutput:
    params = {"to": inp.to, "value": hex(inp.value_wei)}
    if inp.from_address:
        params["from"] = inp.from_address
    if inp.data:
        params["data"] = inp.data
    try:
        hex_result = await call("eth_estimateGas", [params])
    except RpcApplicationError as exc:
        raise RpcComputeError("call_would_revert", str(exc.message))
    return EstimateGasOutput(
        to=inp.to, from_address=inp.from_address, value_wei=inp.value_wei, gas_estimate=int(hex_result, 16),
    )


register(BaseRpcSpec(
    slug="base/estimate-gas", price="$0.004", service_name="base-estimate-gas",
    description="Gas estimate for a Base mainnet call (to/from/value/data) via eth_estimateGas, without broadcasting a transaction.",
    tags=["base rpc", "gas estimate", "eth_estimateGas", "base mainnet", "transaction cost"],
    input_model=EstimateGasInput, output_model=EstimateGasOutput, compute=compute_estimate_gas,
    sample_input={"to": "0xdb6882db2A406Bc1541988715842906Dfd4FD590", "from_address": "0xb3F32bdfe8D07825BC0D7387295aB1D7559BA69d", "value_wei": 0},
    sample_output={"to": "0xdb6882db2A406Bc1541988715842906Dfd4FD590", "from_address": "0xb3F32bdfe8D07825BC0D7387295aB1D7559BA69d", "value_wei": 0, "gas_estimate": 21000},
))
