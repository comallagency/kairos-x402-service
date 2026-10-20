"""base/quote - best swap price across Uniswap v2, Uniswap v3 (4 fee tiers),
and Aerodrome (stable + volatile pools) on Base mainnet, read on-chain via
Multicall3 - no router SDK, no off-chain indexer, no paid price API.

Contract addresses verified on-chain (2026-10-04) before trusting them, not
from documentation alone - all 7 confirmed as real, non-trivial deployed
contracts (eth_getCode), then each DEX's quote function test-called for a
real USDC/WETH amount and cross-checked for a consistent implied price
across all three (~0.000369 ETH per USDC on all three the day this was
written):
  Uniswap V2 Router02:  0x4752ba5dbc23f44d87826276bf6fd6b1c372ad24
  Uniswap V3 Factory:   0x33128a8fC17869897dcE68Ed026d694621f6FDfD (unused
                        directly - QuoterV2 takes tokens+fee, no need to
                        look up the pool address first)
  Uniswap V3 QuoterV2:  0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a
  Aerodrome Router:     0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43
  Aerodrome PoolFactory:0x420DD381b31aEf6683db6B902084cB0FFECe40Da

Uniswap v3's QuoterV2.quoteExactInputSingle is queried across all 4 standard
fee tiers (100/500/3000/10000) since there is no single canonical v3 pool
per pair - picking the best (highest) output across tiers also naturally
discounts a tier with a real pool but near-zero liquidity (measured
directly: the 100 (0.01%) tier for USDC/WETH returned an output ~250,000x
lower than the 500/3000/10000 tiers, i.e. a technically-valid but
functionally-dead pool - max() already ignores it without special-casing).
Aerodrome is queried as both a volatile and a stable pool via one Route
each, since which type exists for a given pair isn't knowable up front.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, field_validator

from app.base_chain.registry import BaseRpcSpec, RpcComputeError, register
from app.base_chain.rpc_client import (
    decode_uint256,
    encode_address_arg,
    encode_uint256_arg,
    is_valid_address,
    multicall,
    selector,
)

UNISWAP_V2_ROUTER = "0x4752ba5dbc23f44d87826276bf6fd6b1c372ad24"
UNISWAP_V3_QUOTER = "0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a"
AERODROME_ROUTER = "0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43"
AERODROME_FACTORY = "0x420DD381b31aEf6683db6B902084cB0FFECe40Da"

V3_FEE_TIERS = (100, 500, 3000, 10000)

_DECIMALS_SEL = selector("decimals()")
_V2_GET_AMOUNTS_OUT_SEL = selector("getAmountsOut(uint256,address[])")
_V3_QUOTE_SEL = selector("quoteExactInputSingle((address,address,uint256,uint24,uint160))")
_AERO_GET_AMOUNTS_OUT_SEL = selector("getAmountsOut(uint256,(address,address,bool,address)[])")


def _check_address(v: str) -> str:
    if not is_valid_address(v):
        raise ValueError("must be a 0x-prefixed 20-byte hex string")
    return v


def _encode_address_array(addrs: list[str]) -> bytes:
    out = encode_uint256_arg(len(addrs))
    for a in addrs:
        out += encode_address_arg(a)
    return out


def _v2_calldata(amount_in: int, token_in: str, token_out: str) -> bytes:
    path = _encode_address_array([token_in, token_out])
    return _V2_GET_AMOUNTS_OUT_SEL + encode_uint256_arg(amount_in) + encode_uint256_arg(0x40) + path


def _v3_calldata(amount_in: int, token_in: str, token_out: str, fee: int) -> bytes:
    return (
        _V3_QUOTE_SEL
        + encode_address_arg(token_in)
        + encode_address_arg(token_out)
        + encode_uint256_arg(amount_in)
        + encode_uint256_arg(fee)
        + encode_uint256_arg(0)
    )


def _aero_calldata(amount_in: int, token_in: str, token_out: str, stable: bool) -> bytes:
    route = (
        encode_address_arg(token_in)
        + encode_address_arg(token_out)
        + encode_uint256_arg(1 if stable else 0)
        + encode_address_arg(AERODROME_FACTORY)
    )
    routes_array = encode_uint256_arg(1) + route
    return _AERO_GET_AMOUNTS_OUT_SEL + encode_uint256_arg(amount_in) + encode_uint256_arg(0x40) + routes_array


def _decode_v2_amounts_out(data: bytes) -> Optional[int]:
    """None for "no answer" (too short to decode) AND for a successfully
    decoded zero - Aerodrome's getAmountsOut, unlike Uniswap V2's, does not
    revert for a route with no pool, it returns a clean 0 (verified
    directly, 2026-10-04) - a zero-output quote is never a usable price
    either way, so both cases collapse to the same "not available" signal
    rather than letting a literal 0 masquerade as a real quote downstream."""
    if len(data) < 128:
        return None
    return decode_uint256(data[96:128]) or None


def _decode_v3_amount_out(data: bytes) -> Optional[int]:
    if len(data) < 32:
        return None
    return decode_uint256(data[0:32]) or None


class QuoteInput(BaseModel):
    token_in: str
    token_out: str
    amount_in: int = Field(gt=0)

    @field_validator("token_in", "token_out")
    @classmethod
    def _validate(cls, v):
        return _check_address(v)


class DexQuote(BaseModel):
    available: bool
    amount_out_raw: Optional[str]
    amount_out: Optional[float]
    detail: Optional[str]


class QuoteOutput(BaseModel):
    token_in: str
    token_out: str
    amount_in: str
    decimals_in: int
    decimals_out: int
    best_dex: Optional[str]
    best_amount_out_raw: Optional[str]
    best_amount_out: Optional[float]
    price_impact_pct: Optional[float]
    uniswap_v2: DexQuote
    uniswap_v3: DexQuote
    aerodrome: DexQuote


def _build_quote_calls(amount_in: int, token_in: str, token_out: str) -> list[tuple[str, bool, bytes]]:
    calls = [(UNISWAP_V2_ROUTER, True, _v2_calldata(amount_in, token_in, token_out))]
    for fee in V3_FEE_TIERS:
        calls.append((UNISWAP_V3_QUOTER, True, _v3_calldata(amount_in, token_in, token_out, fee)))
    calls.append((AERODROME_ROUTER, True, _aero_calldata(amount_in, token_in, token_out, False)))
    calls.append((AERODROME_ROUTER, True, _aero_calldata(amount_in, token_in, token_out, True)))
    return calls


def _best_of(results: list[tuple[bool, bytes]]) -> tuple[Optional[int], Optional[int], Optional[str]]:
    """results: [v2, v3_fee100, v3_fee500, v3_fee3000, v3_fee10000, aero_volatile, aero_stable].
    Returns (v2_out, best_v3_out, best_v3_detail), leaving aero to the caller
    (different decode)."""
    v2_ok, v2_data = results[0]
    v2_out = _decode_v2_amounts_out(v2_data) if v2_ok else None

    best_v3_out, best_v3_fee = None, None
    for i, fee in enumerate(V3_FEE_TIERS):
        ok, data = results[1 + i]
        if not ok:
            continue
        out = _decode_v3_amount_out(data)
        if out is not None and (best_v3_out is None or out > best_v3_out):
            best_v3_out, best_v3_fee = out, fee
    return v2_out, best_v3_out, (f"{best_v3_fee / 10000:.2f}% fee tier" if best_v3_fee else None)


async def compute_quote(inp: QuoteInput) -> QuoteOutput:
    main_calls = [
        (inp.token_in, True, _DECIMALS_SEL),
        (inp.token_out, True, _DECIMALS_SEL),
    ] + _build_quote_calls(inp.amount_in, inp.token_in, inp.token_out)
    results = await multicall(main_calls)

    dec_in_ok, dec_in_data = results[0]
    dec_out_ok, dec_out_data = results[1]
    decimals_in = decode_uint256(dec_in_data) if dec_in_ok else 18
    decimals_out = decode_uint256(dec_out_data) if dec_out_ok else 18

    quote_results = results[2:]
    v2_out, v3_out, v3_detail = _best_of(quote_results)
    aero_v_ok, aero_v_data = quote_results[5]
    aero_s_ok, aero_s_data = quote_results[6]
    aero_v_out = _decode_v2_amounts_out(aero_v_data) if aero_v_ok else None
    aero_s_out = _decode_v2_amounts_out(aero_s_data) if aero_s_ok else None
    aero_out, aero_detail = (None, None)
    if aero_v_out is not None and (aero_s_out is None or aero_v_out >= aero_s_out):
        aero_out, aero_detail = aero_v_out, "volatile pool"
    elif aero_s_out is not None:
        aero_out, aero_detail = aero_s_out, "stable pool"

    def _dex_quote(raw: Optional[int], detail: Optional[str]) -> DexQuote:
        if raw is None:
            return DexQuote(available=False, amount_out_raw=None, amount_out=None, detail=None)
        return DexQuote(
            available=True, amount_out_raw=str(raw), amount_out=raw / (10 ** decimals_out), detail=detail,
        )

    candidates = [("uniswap_v2", v2_out), ("uniswap_v3", v3_out), ("aerodrome", aero_out)]
    available = [(name, out) for name, out in candidates if out is not None]
    if not available:
        raise RpcComputeError(
            "no_liquidity",
            f"no pool found for {inp.token_in} -> {inp.token_out} on Uniswap v2, Uniswap v3, or Aerodrome",
        )
    best_dex, best_out = max(available, key=lambda c: c[1])

    price_impact_pct = None
    if best_dex is not None and inp.amount_in >= 1000:
        ref_amount = max(inp.amount_in // 1000, 1)
        if best_dex == "uniswap_v2":
            ref_calls = [(UNISWAP_V2_ROUTER, True, _v2_calldata(ref_amount, inp.token_in, inp.token_out))]
        elif best_dex == "uniswap_v3":
            ref_calls = [
                (UNISWAP_V3_QUOTER, True, _v3_calldata(ref_amount, inp.token_in, inp.token_out, fee))
                for fee in V3_FEE_TIERS
            ]
        else:
            ref_calls = [
                (AERODROME_ROUTER, True, _aero_calldata(ref_amount, inp.token_in, inp.token_out, False)),
                (AERODROME_ROUTER, True, _aero_calldata(ref_amount, inp.token_in, inp.token_out, True)),
            ]
        ref_results = await multicall(ref_calls)
        if best_dex == "uniswap_v2":
            ref_ok, ref_data = ref_results[0]
            ref_out = _decode_v2_amounts_out(ref_data) if ref_ok else None
        elif best_dex == "uniswap_v3":
            ref_out = None
            for ok, data in ref_results:
                if ok:
                    out = _decode_v3_amount_out(data)
                    if out is not None and (ref_out is None or out > ref_out):
                        ref_out = out
        else:
            r_outs = [
                _decode_v2_amounts_out(data) if ok else None for ok, data in ref_results
            ]
            ref_out = max((o for o in r_outs if o is not None), default=None)
        if ref_out:
            ref_rate = ref_out / ref_amount
            actual_rate = best_out / inp.amount_in
            price_impact_pct = round((1 - actual_rate / ref_rate) * 100, 4)

    return QuoteOutput(
        token_in=inp.token_in, token_out=inp.token_out, amount_in=str(inp.amount_in),
        decimals_in=decimals_in, decimals_out=decimals_out,
        best_dex=best_dex,
        best_amount_out_raw=str(best_out) if best_out is not None else None,
        best_amount_out=(best_out / (10 ** decimals_out)) if best_out is not None else None,
        price_impact_pct=price_impact_pct,
        uniswap_v2=_dex_quote(v2_out, None),
        uniswap_v3=_dex_quote(v3_out, v3_detail),
        aerodrome=_dex_quote(aero_out, aero_detail),
    )


register(BaseRpcSpec(
    slug="base/quote", price="$0.003", service_name="base-quote",
    description="Slippage estimate, swap quote, and best DEX price on Base - compares Uniswap v2, Uniswap v3, and Aerodrome on-chain via Multicall3, returns amount received, chosen path, and price impact.",
    tags=["swap quote base", "best dex price base", "base rpc", "uniswap aerodrome", "slippage estimate"],
    input_model=QuoteInput, output_model=QuoteOutput, compute=compute_quote,
    sample_input={
        "token_in": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        "token_out": "0x4200000000000000000000000000000000000006",
        "amount_in": 1000000,
    },
    sample_output={
        "token_in": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "token_out": "0x4200000000000000000000000000000000000006",
        "amount_in": "1000000", "decimals_in": 6, "decimals_out": 18,
        "best_dex": "uniswap_v3", "best_amount_out_raw": "370394821572273", "best_amount_out": 0.000370394821572273,
        "price_impact_pct": 0.01,
        "uniswap_v2": {"available": True, "amount_out_raw": "369235162467701", "amount_out": 0.000369235162467701, "detail": None},
        "uniswap_v3": {"available": True, "amount_out_raw": "370394821572273", "amount_out": 0.000370394821572273, "detail": "0.05% fee tier"},
        "aerodrome": {"available": True, "amount_out_raw": "368869375130808", "amount_out": 0.000368869375130808, "detail": "volatile pool"},
    },
))
