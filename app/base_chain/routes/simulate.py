"""base/simulate - will a proposed call succeed, how much gas, and why not if
it reverts. See app/base_chain/registry.py.

Base's public RPC providers (all 3 in rpc_client._PROVIDER_ORDER) strip
revert data from a plain eth_call's JSON-RPC error - verified directly
(2026-10-04): a call guaranteed to revert with a real Error(string) reason
(a USDC transfer() with no balance) came back as a bare "execution reverted"
with no `data` field on all three. Fix: route the SAME call through
Multicall3.aggregate3 with allowFailure=true - Multicall3 catches the revert
internally (a low-level .call()) and returns it as ordinary, non-error
returnData, confirmed to carry the real payload the provider otherwise
discards (that same USDC call round-trips to the exact "ERC20: transfer
amount exceeds balance" string this way). Only attempted for value=0:
forwarding real value through Multicall3 would need Multicall3 itself to
hold the funds, which it never does - value-bearing reverts get an honest
"unavailable" note instead of a guess.

The primary will_succeed/gas_estimate signal still comes from a DIRECT
eth_call/eth_estimateGas using the caller's real `from` - proxying through
Multicall3 changes msg.sender to Multicall3's own address, which would
silently give the wrong answer for any contract logic that is
msg.sender-dependent (the whole point of letting the caller specify `from`
in the first place).
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, field_validator

from app.base_chain.registry import BaseRpcSpec, register
from app.base_chain.rpc_client import (
    RpcApplicationError,
    call,
    decode_revert_reason,
    gather,
    is_valid_address,
    multicall,
)


def _check_address(v: str) -> str:
    if not is_valid_address(v):
        raise ValueError("must be a 0x-prefixed 20-byte hex string")
    return v


class SimulateInput(BaseModel):
    from_address: str
    to: str
    data: str = "0x"
    value_wei: int = 0

    @field_validator("from_address", "to")
    @classmethod
    def _validate_address(cls, v):
        return _check_address(v)

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


class SimulateOutput(BaseModel):
    will_succeed: bool
    gas_estimate: Optional[int]
    revert_reason: Optional[str]
    return_data: Optional[str]


async def compute_simulate(inp: SimulateInput) -> SimulateOutput:
    call_obj = {"from": inp.from_address, "to": inp.to, "data": inp.data}
    if inp.value_wei:
        call_obj["value"] = hex(inp.value_wei)

    async def _try_call():
        try:
            return True, await call("eth_call", [call_obj, "latest"])
        except RpcApplicationError:
            return False, None

    async def _try_gas():
        try:
            return True, await call("eth_estimateGas", [call_obj])
        except RpcApplicationError:
            return False, None

    (call_ok, call_result), (gas_ok, gas_result) = await gather(_try_call(), _try_gas())

    if call_ok:
        return SimulateOutput(
            will_succeed=True,
            gas_estimate=int(gas_result, 16) if gas_ok else None,
            revert_reason=None,
            return_data=call_result,
        )

    if inp.value_wei == 0:
        results = await multicall([(inp.to, True, bytes.fromhex(inp.data[2:]))])
        ok, data = results[0]
        revert_reason = None if ok else decode_revert_reason(data)
    else:
        revert_reason = (
            "revert reason unavailable for value-bearing calls - recovering it would "
            "require Multicall3 to hold real funds, which this pack never does"
        )

    return SimulateOutput(will_succeed=False, gas_estimate=None, revert_reason=revert_reason, return_data=None)


register(BaseRpcSpec(
    slug="base/simulate", price="$0.005", service_name="base-simulate",
    description="Simulate a Base mainnet transaction before sending it - will it succeed, estimated gas, and the decoded revert reason if it would fail.",
    tags=["simulate transaction base", "transaction simulation", "base rpc", "eth_call", "revert reason"],
    input_model=SimulateInput, output_model=SimulateOutput, compute=compute_simulate,
    sample_input={
        "from_address": "0xb3F32bdfe8D07825BC0D7387295aB1D7559BA69d",
        "to": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        "data": "0x313ce567",
    },
    sample_output={
        "will_succeed": True, "gas_estimate": 30958, "revert_reason": None,
        "return_data": "0x0000000000000000000000000000000000000000000000000000000000000006",
    },
))
