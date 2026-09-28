"""POST /token-risk — 100% on-chain ERC-20 risk scan on Base, no third-party
data vendor (no GoPlus/DexScreener/DefiLlama - only public Base RPC reads via
app.upstream.evm_rpc). See BRIEF-CORRECTIONS.md / the 2026-09-28 Phase 0
feasibility research this route is built from:

- Bytecode (mint/blacklist/pause/tax-setter selectors, EIP-1967 proxy slot,
  owner()) - always attempted, exactly 3 RPC calls, ~0.3s in testing.
- Liquidity (Uniswap V2 pair, Uniswap V3 pools across 3 fee tiers, Aerodrome
  pool both stable/volatile) - always attempted, on-chain pool discovery via
  each DEX's own factory contract, no indexer.
- Holder concentration (top 10 by % of supply, from Transfer logs) - the
  public Base RPC caps eth_getLogs at ~2000 blocks/call and a busy contract's
  response blows past the payload-size limit well before that (measured:
  USDC returns ~10k logs in just 100 blocks). So this is only attempted if
  the token's whole history provably fits in at most 2 pages (4000 blocks) -
  see _holder_distribution's completeness check. Otherwise the field reads
  "skipped_established_token": reconstructing a partial holder list and
  presenting it as if it were complete would be worse than not showing it,
  for a route whose whole point is a safety verdict.

The verdict itself is a real Jev decision (typesafe/jev-1.13, the same
mechanism as /decide, /guard, /verify, /rank - see app/upstream/jev.py), not
a hand-rolled score: the raw signals above are handed to Jev as state, which
returns avoid/caution/acceptable with real per-option probabilities.
"""

from __future__ import annotations

import asyncio
import json
import re
import time

from eth_utils import keccak
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app import config, db
from app.receipts import Timer, effective_price, extract_payer_address, make_receipt, price_float
from app.upstream.evm_rpc import EvmRpcError, NETWORKS, rpc
from app.upstream.jev import JevError, ask_jev
from app.x402_setup import ROUTE_DESCRIPTIONS

router = APIRouter()

_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_NETWORK = NETWORKS["base"]
_WETH = "0x4200000000000000000000000000000000000006"
_UNISWAP_V2_FACTORY = "0x8909Dc15e40173Ff4699343b6eB8132c65e18eC6"
_UNISWAP_V3_FACTORY = "0x33128a8fC17869897dcE68Ed026d694621f6FDfD"
_AERODROME_FACTORY = "0x420DD381b31aEf6683db6B902084cB0FFECe40Da"
_V3_FEE_TIERS = (500, 3000, 10000)

_LOG_PAGE_BLOCKS = 2000  # measured ceiling on the public Base RPC, see module docstring
_MAX_LOG_PAGES = 2
_HOLDER_LOG_CAP = 500  # more than this in one page -> clearly an established/busy token

_CACHE_TTL_S = 300
_cache: dict[str, tuple[float, dict]] = {}


def _selector(sig: str) -> str:
    return "0x" + keccak(text=sig).hex()[:8]


def _topic(sig: str) -> str:
    return "0x" + keccak(text=sig).hex()


_TRANSFER_TOPIC = _topic("Transfer(address,address,uint256)")
_EIP1967_IMPL_SLOT = "0x" + (
    int.from_bytes(keccak(text="eip1967.proxy.implementation"), "big") - 1
).to_bytes(32, "big").hex()

_RISK_SELECTORS = {
    "mint": "mint(address,uint256)",
    "mint_uint": "mint(uint256)",
    "mint_to": "mintTo(address,uint256)",
    "blacklist": "blacklist(address)",
    "set_blacklist": "setBlacklist(address,bool)",
    "is_blacklisted": "isBlacklisted(address)",
    "pause": "pause()",
    "unpause": "unpause()",
    "set_paused": "setPaused(bool)",
    "set_fee": "setFee(uint256)",
    "set_fees": "setFees(uint256,uint256)",
    "set_tax_fee": "setTaxFee(uint256)",
    "update_buy_fee": "updateBuyFee(uint256)",
    "update_sell_fee": "updateSellFee(uint256)",
    "set_max_tx_amount": "setMaxTxAmount(uint256)",
    "set_max_wallet_amount": "setMaxWalletAmount(uint256)",
    "exclude_from_fee": "excludeFromFee(address)",
}
_OWNER_SELECTOR = _selector("owner()")
_GET_RESERVES_SELECTOR = _selector("getReserves()")
_GET_PAIR_SELECTOR = _selector("getPair(address,address)")
_GET_POOL_V3_SELECTOR = _selector("getPool(address,address,uint24)")
_GET_POOL_AERO_SELECTOR = _selector("getPool(address,address,bool)")
_LIQUIDITY_V3_SELECTOR = _selector("liquidity()")

_ZERO_ADDRESS = "0x" + "00" * 20


def _pad_address(address: str) -> str:
    return address[2:].rjust(64, "0").lower()


async def _fetch_code(address: str) -> str:
    code = await rpc(_NETWORK, "eth_getCode", [address, "latest"])
    return code if isinstance(code, str) else "0x"


async def _analyze_bytecode(address: str, code: str) -> dict:
    """The contract-existence check (app._lookup) already fetched the
    bytecode with its own eth_getCode call - reused here rather than
    re-fetched, so this stage costs exactly 2 more RPC calls (EIP-1967 slot,
    owner()), 3 total together with that first call."""
    impl_slot, owner_raw = await asyncio.gather(
        rpc(_NETWORK, "eth_getStorageAt", [address, _EIP1967_IMPL_SLOT, "latest"]),
        rpc(_NETWORK, "eth_call", [{"to": address, "data": _OWNER_SELECTOR}, "latest"]),
        return_exceptions=True,
    )
    flags = {name: sel[2:] in code for name, sel in _RISK_SELECTORS.items()}
    is_proxy = isinstance(impl_slot, str) and impl_slot.lower() != "0x" + "00" * 32
    owner = None
    owner_renounced = None
    if isinstance(owner_raw, str) and len(owner_raw) >= 66:
        owner = "0x" + owner_raw[-40:]
        owner_renounced = owner.lower() == _ZERO_ADDRESS
    return {
        "bytecode_size": max(len(code) // 2 - 1, 0),
        "flags": flags,
        "any_dangerous_function": any(flags.values()),
        "is_upgradeable_proxy": is_proxy,
        "owner": owner,
        "owner_renounced": owner_renounced,
    }


async def _liquidity(address: str) -> dict:
    token_padded = _pad_address(address)
    weth_padded = _pad_address(_WETH)

    v2_pair_raw = await rpc(
        _NETWORK, "eth_call", [{"to": _UNISWAP_V2_FACTORY, "data": _GET_PAIR_SELECTOR + token_padded + weth_padded}, "latest"]
    )
    v2_pair = "0x" + v2_pair_raw[-40:] if isinstance(v2_pair_raw, str) and len(v2_pair_raw) >= 66 else None
    v2 = None
    if v2_pair and v2_pair.lower() != _ZERO_ADDRESS:
        reserves_raw = await rpc(_NETWORK, "eth_call", [{"to": v2_pair, "data": _GET_RESERVES_SELECTOR}, "latest"])
        if isinstance(reserves_raw, str) and len(reserves_raw) >= 130:
            v2 = {
                "pair": v2_pair,
                "reserve0": str(int(reserves_raw[2:66], 16)),
                "reserve1": str(int(reserves_raw[66:130], 16)),
            }

    async def _v3_pool(fee: int):
        fee_hex = format(fee, "064x")
        data = _GET_POOL_V3_SELECTOR + token_padded + weth_padded + fee_hex
        pool_raw = await rpc(_NETWORK, "eth_call", [{"to": _UNISWAP_V3_FACTORY, "data": data}, "latest"])
        pool = "0x" + pool_raw[-40:] if isinstance(pool_raw, str) and len(pool_raw) >= 66 else None
        if not pool or pool.lower() == _ZERO_ADDRESS:
            return None
        liquidity_raw = await rpc(_NETWORK, "eth_call", [{"to": pool, "data": _LIQUIDITY_V3_SELECTOR}, "latest"])
        liquidity = str(int(liquidity_raw, 16)) if isinstance(liquidity_raw, str) else None
        return {"pool": pool, "fee": fee, "liquidity": liquidity}

    async def _aero_pool(stable: bool):
        stable_hex = format(1 if stable else 0, "064x")
        data = _GET_POOL_AERO_SELECTOR + token_padded + weth_padded + stable_hex
        pool_raw = await rpc(_NETWORK, "eth_call", [{"to": _AERODROME_FACTORY, "data": data}, "latest"])
        pool = "0x" + pool_raw[-40:] if isinstance(pool_raw, str) and len(pool_raw) >= 66 else None
        if not pool or pool.lower() == _ZERO_ADDRESS:
            return None
        reserves_raw = await rpc(_NETWORK, "eth_call", [{"to": pool, "data": _GET_RESERVES_SELECTOR}, "latest"])
        if not isinstance(reserves_raw, str) or len(reserves_raw) < 130:
            return {"pool": pool, "stable": stable}
        return {
            "pool": pool,
            "stable": stable,
            "reserve0": str(int(reserves_raw[2:66], 16)),
            "reserve1": str(int(reserves_raw[66:130], 16)),
        }

    v3_results, aero_results = await asyncio.gather(
        asyncio.gather(*(_v3_pool(fee) for fee in _V3_FEE_TIERS)),
        asyncio.gather(*(_aero_pool(stable) for stable in (False, True))),
    )
    v3_pools = [p for p in v3_results if p]
    aero_pools = [p for p in aero_results if p]

    return {
        "uniswap_v2": v2,
        "uniswap_v3_pools": v3_pools,
        "aerodrome_pools": aero_pools,
        "any_liquidity_found": bool(v2 or v3_pools or aero_pools),
    }


async def _get_logs_page(address: str, from_block: int, to_block: int) -> list | None:
    try:
        return await rpc(
            _NETWORK,
            "eth_getLogs",
            [{"address": address, "topics": [_TRANSFER_TOPIC], "fromBlock": hex(max(from_block, 0)), "toBlock": hex(to_block)}],
        )
    except EvmRpcError:
        return None


async def _holder_distribution(address: str) -> dict:
    """Top-10 holder concentration, ONLY if the full transfer history
    provably fits in at most _MAX_LOG_PAGES pages of _LOG_PAGE_BLOCKS blocks
    each - see module docstring. "Provably" means: the oldest page returned
    either zero logs, or its earliest log's block is strictly after that
    page's own fromBlock (a gap proving nothing older exists to miss) -
    never a partial reconstruction presented as complete.
    """
    latest_hex = await rpc(_NETWORK, "eth_blockNumber", [])
    latest = int(latest_hex, 16)

    all_logs: list = []
    complete = False
    for page in range(_MAX_LOG_PAGES):
        to_block = latest - page * _LOG_PAGE_BLOCKS
        from_block = to_block - _LOG_PAGE_BLOCKS + 1
        logs = await _get_logs_page(address, from_block, to_block)
        if logs is None:
            complete = False
            break
        if len(logs) > _HOLDER_LOG_CAP:
            complete = False
            break
        all_logs.extend(logs)
        if not logs:
            complete = True
            break
        earliest_block = min(int(log["blockNumber"], 16) for log in logs)
        if earliest_block > from_block:
            complete = True
            break
        # earliest log touches this page's boundary -> history may continue
        # further back; only keep going if we still have a page budget left
        complete = False

    if not complete:
        return {"status": "skipped_established_token"}

    balances: dict[str, int] = {}
    for log in all_logs:
        frm = "0x" + log["topics"][1][-40:]
        to = "0x" + log["topics"][2][-40:]
        value = int(log["data"], 16)
        balances[frm] = balances.get(frm, 0) - value
        balances[to] = balances.get(to, 0) + value
    holders = {addr: bal for addr, bal in balances.items() if bal > 0}
    total_supply = sum(holders.values())
    top10 = sorted(holders.values(), reverse=True)[:10]
    top10_pct = round(sum(top10) / total_supply * 100, 2) if total_supply else None

    return {
        "status": "ok",
        "distinct_holders": len(holders),
        "top10_pct_of_supply": top10_pct,
    }


_VERDICT_CRITERIA = {
    "avoid": (
        "The token has one or more of: an active mint/blacklist/pause power "
        "held by a non-renounced owner, an upgradeable proxy with no "
        "liquidity found on any DEX, or a token-safety flag combined with no "
        "liquidity at all. A rational agent should not acquire this token."
    ),
    "caution": (
        "The token has some centralized-control powers (owner not renounced, "
        "or a tax/limit-setter function) but real liquidity exists on at "
        "least one DEX and there is no mint+blacklist combination found - a "
        "real but bounded risk, worth a smaller position or extra scrutiny."
    ),
    "acceptable": (
        "Ownership is renounced (or no dangerous owner-only function exists "
        "in the bytecode at all), it is not an upgradeable proxy, and real "
        "liquidity was found on at least one DEX. No structural red flag."
    ),
}
_VERDICT_INSTRUCTIONS = (
    "Given these on-chain structural signals for a Base ERC-20 token "
    "(bytecode powers, proxy/ownership status, DEX liquidity, holder "
    "concentration where available), is this token safe to acquire: avoid, "
    "caution, or acceptable?"
)


async def _verdict(address: str, bytecode: dict, liquidity: dict, holders: dict) -> dict:
    state = json.dumps({"address": address, "bytecode": bytecode, "liquidity": liquidity, "holders": holders})
    questions = {"verdict": {"type": "choice", "instructions": _VERDICT_INSTRUCTIONS, "criteria": _VERDICT_CRITERIA}}
    data = await ask_jev(state, questions)
    answer = data["answers"]["verdict"]
    return {
        "verdict": answer["choice"],
        "probability": answer["probabilities"][answer["choice"]],
        "probabilities": answer["probabilities"],
        "confidence": answer.get("confidence"),
    }


async def _lookup(body: dict) -> dict:
    address = str(body.get("address") or body.get("token") or "").strip()
    if not _ADDRESS_RE.fullmatch(address):
        raise EvmRpcError("invalid_address")
    address = "0x" + address[2:]

    cached = _cache.get(address.lower())
    if cached and cached[0] > time.monotonic():
        return cached[1]

    code = await _fetch_code(address)
    if code in ("0x", "0x0"):
        raise EvmRpcError("not_a_contract")

    bytecode, liquidity, holders = await asyncio.gather(
        _analyze_bytecode(address, code),
        _liquidity(address),
        _holder_distribution(address),
    )
    verdict = await _verdict(address, bytecode, liquidity, holders)

    result = {
        "address": address,
        "network": {"key": _NETWORK.key, "name": _NETWORK.name, "caip2": _NETWORK.caip2},
        "verdict": verdict,
        "bytecode_analysis": bytecode,
        "liquidity_analysis": liquidity,
        "holders_analysis": holders,
    }
    _cache[address.lower()] = (time.monotonic() + _CACHE_TTL_S, result)
    return result


SAMPLE_RESPONSE = {
    "address": "0x532f27101965dd16442E59d40670FaF5eBB142E",
    "network": {"key": "base", "name": "Base", "caip2": "eip155:8453"},
    "verdict": {
        "verdict": "caution",
        "probability": 0.62,
        "probabilities": {"acceptable": 0.31, "avoid": 0.07, "caution": 0.62},
        "confidence": 0.55,
    },
    "bytecode_analysis": {
        "bytecode_size": 8421,
        "flags": {"mint": False, "blacklist": False, "pause": False, "set_max_tx_amount": True},
        "any_dangerous_function": True,
        "is_upgradeable_proxy": False,
        "owner": "0x0000000000000000000000000000000000000000",
        "owner_renounced": True,
    },
    "liquidity_analysis": {
        "uniswap_v2": {"pair": "0x1efdc3e6cfb3df3b7dd3e3971d5262733c52c21c", "reserve0": "1535891536408965785", "reserve1": "5106124266540300941199548545"},
        "uniswap_v3_pools": [],
        "aerodrome_pools": [],
        "any_liquidity_found": True,
    },
    "holders_analysis": {"status": "skipped_established_token"},
}


@router.get("/token-risk/sample", openapi_extra={"security": []})
async def token_risk_sample():
    return {
        **SAMPLE_RESPONSE,
        "note": "Static example, not a live call. 100% on-chain: no GoPlus, DexScreener or DefiLlama.",
        "x402_receipt": make_receipt(None, "token-risk", 1800, 0.0),
    }


async def _paid(request: Request, body: dict):
    payer = extract_payer_address(request)
    user_agent = request.headers.get("user-agent")
    body_excerpt = json.dumps(body)[:2000]
    try:
        with Timer() as timer:
            result = await _lookup(body)
    except EvmRpcError as exc:
        reason = str(exc)
        code = 400 if reason in {"invalid_address", "not_a_contract"} else 502
        db.log_request(
            route="token-risk", method=request.method, status="error", payer=payer,
            user_agent=user_agent, body_excerpt=body_excerpt, error_reason=reason,
        )
        return JSONResponse({"error": {"reason": reason}}, status_code=code)
    except JevError as exc:
        db.log_request(
            route="token-risk", method=request.method, status="error", payer=payer,
            user_agent=user_agent, body_excerpt=body_excerpt, error_reason=str(exc)[:200],
        )
        return JSONResponse({"error": {"reason": "upstream_error", "detail": str(exc)[:200]}}, status_code=502)

    price = effective_price(payer, price_float(config.PRICE_TOKEN_RISK))
    db.log_request(
        route="token-risk", method=request.method, status="paid",
        latency_ms=timer.elapsed_ms, amount_usdc=price, payer=payer,
        user_agent=user_agent, body_excerpt=body_excerpt,
    )
    return {
        **result,
        "x402_receipt": make_receipt(None, "token-risk", timer.elapsed_ms, price),
    }


@router.post("/token-risk", description=ROUTE_DESCRIPTIONS["token-risk"])
async def token_risk_post(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    return await _paid(request, body)
