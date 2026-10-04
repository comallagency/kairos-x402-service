"""base/can-sell - simulates a real buy THEN a real sell of a token in one
atomic on-chain call, to answer "can I actually sell this?" factually, never
predictively. See app/base_chain/registry.py.

Standard honeypot-detection technique, built from two primitives verified
directly against Base mainnet (2026-10-04):
1. eth_call's third parameter (state override) is supported by all 3
   providers in rpc_client._PROVIDER_ORDER - confirmed by overriding an
   address's ETH balance and reading it back via Multicall3.getEthBalance()
   within the same call. Needed because Multicall3 holds no real ETH of its
   own to spend on a test buy.
2. Multicall3.aggregate3Value lets a buy (ETH -> token, via Uniswap V2,
   crediting Multicall3), an approve (token -> router), and a sell (token ->
   WETH -> USDC, via Uniswap V2) execute as ONE atomic unit, each seeing the
   state changes made by the ones before it - this is the only way to make
   "sell what was just bought" true within a single eth_call at all (every
   call is otherwise fully independent/stateless).

The sell leg routes to USDC, not back to ETH, deliberately: a first version
sold token -> WETH and had the router send the proceeds back to Multicall3
as raw ETH, which reverted every single time with "TransferHelper:
ETH_TRANSFER_FAILED" - Multicall3 has no receive()/payable fallback, so it
can never accept a plain ETH transfer. Routing the final leg through WETH to
USDC (an ERC20 transfer, which needs nothing special to receive) fixed it;
verified against a real, liquid, legitimately-sellable token (DEGEN) before
trusting the mechanism.

Scope, stated plainly: this only tests the ETH -> token -> USDC path via
Uniswap V2. A token with real liquidity ONLY against USDC (no WETH pool at
all) would show as "can't even buy" here, which is a limitation of this
route, not evidence the token is a honeypot - Base's dominant convention is
a WETH pool, but it is not universal. The verdict is reported as
buy_succeeded/sell_succeeded separately so this distinction is never lost in
a single collapsed "yes/no".
"""

from __future__ import annotations

import time
from typing import Optional

from pydantic import BaseModel, Field, field_validator

from app.base_chain.registry import BaseRpcSpec, RpcComputeError, register
from app.base_chain.rpc_client import (
    MULTICALL3,
    decode_revert_reason,
    decode_uint256,
    encode_address_arg,
    encode_uint256_arg,
    is_valid_address,
    multicall,
    multicall_value,
    selector,
)

UNISWAP_V2_ROUTER = "0x4752ba5dbc23f44d87826276bf6fd6b1c372ad24"
WETH = "0x4200000000000000000000000000000000000006"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
DEFAULT_TEST_ETH_WEI = 10**16  # 0.01 ETH - large enough to clear most minimum-liquidity reverts
FAKE_ETH_BALANCE = hex(10**20)  # 100 ETH, state-overridden onto Multicall3 for the test buy
DEADLINE_WINDOW_S = 600
SELL_SAFETY_MARGIN_BPS = 9500  # sell 95% of the predicted buy output, margin for transfer tax

_GET_AMOUNTS_OUT_SEL = selector("getAmountsOut(uint256,address[])")
_BUY_SEL = selector("swapExactETHForTokensSupportingFeeOnTransferTokens(uint256,address[],address,uint256)")
_SELL_SEL = selector("swapExactTokensForTokensSupportingFeeOnTransferTokens(uint256,uint256,address[],address,uint256)")
_APPROVE_SEL = selector("approve(address,uint256)")
_MAX_UINT256 = 2**256 - 1


def _encode_address_array(addrs: list[str]) -> bytes:
    out = encode_uint256_arg(len(addrs))
    for a in addrs:
        out += encode_address_arg(a)
    return out


class CanSellInput(BaseModel):
    token: str
    test_eth_wei: int = Field(default=DEFAULT_TEST_ETH_WEI, gt=0)

    @field_validator("token")
    @classmethod
    def _validate(cls, v):
        if not is_valid_address(v):
            raise ValueError("must be a 0x-prefixed 20-byte hex string")
        return v


class CanSellOutput(BaseModel):
    token: str
    test_eth_wei: str
    buy_succeeded: bool
    approve_succeeded: Optional[bool]
    sell_succeeded: Optional[bool]
    can_sell: Optional[bool]
    reason: Optional[str]


async def compute_can_sell(inp: CanSellInput) -> CanSellOutput:
    path_bytes = encode_uint256_arg(0x40) + _encode_address_array([WETH, inp.token])
    quote_calldata = _GET_AMOUNTS_OUT_SEL + encode_uint256_arg(inp.test_eth_wei) + path_bytes
    quote_results = await multicall([(UNISWAP_V2_ROUTER, True, quote_calldata)])
    quote_ok, quote_data = quote_results[0]
    if not quote_ok or len(quote_data) < 128:
        raise RpcComputeError(
            "no_liquidity",
            f"no Uniswap V2 WETH pool (or no liquidity) found for {inp.token} - cannot even quote a buy",
        )
    predicted_out = decode_uint256(quote_data[96:128])
    if predicted_out == 0:
        raise RpcComputeError("no_liquidity", f"Uniswap V2 quote for {inp.token} returned zero output")
    sell_amount = predicted_out * SELL_SAFETY_MARGIN_BPS // 10000

    deadline = int(time.time()) + DEADLINE_WINDOW_S

    buy_calldata = (
        _BUY_SEL
        + encode_uint256_arg(0)
        + encode_uint256_arg(0x80)
        + encode_address_arg(MULTICALL3)
        + encode_uint256_arg(deadline)
        + _encode_address_array([WETH, inp.token])
    )
    approve_calldata = _APPROVE_SEL + encode_address_arg(UNISWAP_V2_ROUTER) + encode_uint256_arg(_MAX_UINT256)
    sell_calldata = (
        _SELL_SEL
        + encode_uint256_arg(sell_amount)
        + encode_uint256_arg(0)
        + encode_uint256_arg(0xA0)
        + encode_address_arg(MULTICALL3)
        + encode_uint256_arg(deadline)
        + _encode_address_array([inp.token, WETH, USDC])
    )

    calls = [
        (UNISWAP_V2_ROUTER, True, inp.test_eth_wei, buy_calldata),
        (inp.token, True, 0, approve_calldata),
        (UNISWAP_V2_ROUTER, True, 0, sell_calldata),
    ]
    results = await multicall_value(calls, state_override={MULTICALL3: {"balance": FAKE_ETH_BALANCE}})
    (buy_ok, buy_data), (approve_ok, approve_data), (sell_ok, sell_data) = results

    if not buy_ok:
        return CanSellOutput(
            token=inp.token, test_eth_wei=str(inp.test_eth_wei), buy_succeeded=False,
            approve_succeeded=None, sell_succeeded=None, can_sell=None,
            reason=f"buy failed: {decode_revert_reason(buy_data)}",
        )
    if not approve_ok:
        return CanSellOutput(
            token=inp.token, test_eth_wei=str(inp.test_eth_wei), buy_succeeded=True,
            approve_succeeded=False, sell_succeeded=None, can_sell=None,
            reason=f"bought successfully, but approve() failed: {decode_revert_reason(approve_data)}",
        )
    if not sell_ok:
        return CanSellOutput(
            token=inp.token, test_eth_wei=str(inp.test_eth_wei), buy_succeeded=True,
            approve_succeeded=True, sell_succeeded=False, can_sell=False,
            reason=f"bought successfully, but selling reverted: {decode_revert_reason(sell_data)}",
        )
    return CanSellOutput(
        token=inp.token, test_eth_wei=str(inp.test_eth_wei), buy_succeeded=True,
        approve_succeeded=True, sell_succeeded=True, can_sell=True,
        reason=None,
    )


register(BaseRpcSpec(
    slug="base/can-sell", price="$0.005", service_name="base-can-sell",
    description="Can I sell this token? Honeypot check that simulates a real buy then a real sell of a Base mainnet token in one atomic on-chain call via Uniswap V2 - if the sell reverts, that is a honeypot, reported with the exact decoded reason. Factual, never predictive.",
    tags=["honeypot check", "can i sell this token", "base rpc", "rug check", "uniswap v2"],
    input_model=CanSellInput, output_model=CanSellOutput, compute=compute_can_sell,
    sample_input={"token": "0x4ed4E862860beD51a9570b96d89aF5E1B0Efefed", "test_eth_wei": 10000000000000000},
    sample_output={
        "token": "0x4ed4E862860beD51a9570b96d89aF5E1B0Efefed", "test_eth_wei": "10000000000000000",
        "buy_succeeded": True, "approve_succeeded": True, "sell_succeeded": True, "can_sell": True, "reason": None,
    },
))
