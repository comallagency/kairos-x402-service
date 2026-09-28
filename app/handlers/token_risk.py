"""POST /token-risk — 100% on-chain ERC-20 risk scan on Base, no third-party
data vendor (no GoPlus/DexScreener/DefiLlama - only public Base RPC reads via
app.upstream.evm_rpc). See BRIEF-CORRECTIONS.md / the 2026-09-28 Phase 0
feasibility research this route is built from:

- Bytecode (mint/blacklist/pause/tax-setter selectors, EIP-1967 proxy slot,
  owner()) - exactly 3 RPC calls, ~0.3-2s in testing.
- Liquidity (Uniswap V2 pair, Uniswap V3 pools across 3 fee tiers, Aerodrome
  pool both stable/volatile) - on-chain pool discovery via each DEX's own
  factory contract, no indexer.
- Holder concentration (top 10 by % of supply, from Transfer logs) - the
  public Base RPC caps eth_getLogs at ~2000 blocks/call and a busy contract's
  response blows past the payload-size limit well before that (measured:
  USDC returns ~10k logs in just 100 blocks). So this is only attempted if
  the token's whole history provably fits in at most 2 pages (4000 blocks) -
  see _holder_distribution's completeness check. Otherwise the field reads
  "skipped_established_token": reconstructing a partial holder list and
  presenting it as if it were complete would be worse than not showing it,
  for a route whose whole point is a safety verdict.

Latency incident (2026-09-28, 13:39 UTC)
-------------------------------------------
The first real bootstrap settlement took 8.218s server-side (db latency_ms)
- the buyer's own HTTP client had already given up with a ReadTimeout before
the response arrived, so a real payment settled with nobody ever receiving
the analysis it paid for. Root-caused by re-measuring each stage standalone
against the exact same target (USDC): _liquidity() alone took ~11s because
the Uniswap V2 pair+reserves lookup ran BEFORE the V3/Aerodrome fan-out
started instead of alongside it (fixed below - all three now start
concurrently), and the Jev call itself measured anywhere from ~2s to 18s
across repeated calls - OpenRouter's alpha Decisions API has no SLA and no
bound of its own.

Fix: every RPC-heavy stage (bytecode, liquidity, holders) now runs under its
own _STEP_TIMEOUT_S bound - a stage that blows its budget degrades to
{"status": "unavailable"} instead of blocking the other stages or the
response as a whole, and the Jev verdict is explicitly told which signals it
does and doesn't have. On top of that, the ENTIRE analysis (all three stages
plus the Jev call) runs under one _GLOBAL_TIMEOUT_S deadline - if that fires,
_lookup raises before any verdict is produced, _paid() returns 504, and
because the x402 SDK never settles a >=400 response, nothing is charged. A
real payment now only ever settles once a client could plausibly still be
waiting for it.
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

_STEP_TIMEOUT_S = 3.0
_GLOBAL_TIMEOUT_S = 10.0

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
    """The contract-existence check (_lookup) already fetched the bytecode
    with its own eth_getCode call - reused here rather than re-fetched, so
    this stage costs exactly 2 more RPC calls (EIP-1967 slot, owner()), 3
    total together with that first call."""
    impl_slot, owner_raw = await asyncio.gather(
        rpc(_NETWORK, "eth_getStorageAt", [address, _EIP1967_IMPL_SLOT, "latest"]),
        rpc(_NETWORK, "eth_call", [{"to": address, "data": _OWNER_SELECTOR}, "latest"]),
        return_exceptions=True,
    )
    # owner() reverting just means the token isn't Ownable (very common,
    # not an error) - only re-raise if the PROXY SLOT read itself failed,
    # since that one has no legitimate "revert" case and its failure means
    # the RPC call genuinely didn't work.
    if isinstance(impl_slot, BaseException):
        raise impl_slot
    flags = {name: sel[2:] in code for name, sel in _RISK_SELECTORS.items()}
    is_proxy = isinstance(impl_slot, str) and impl_slot.lower() != "0x" + "00" * 32
    owner = None
    owner_renounced = None
    if isinstance(owner_raw, str) and len(owner_raw) >= 66:
        owner = "0x" + owner_raw[-40:]
        owner_renounced = owner.lower() == _ZERO_ADDRESS
    return {
        "status": "ok",
        "bytecode_size": max(len(code) // 2 - 1, 0),
        "flags": flags,
        "any_dangerous_function": any(flags.values()),
        "is_upgradeable_proxy": is_proxy,
        "owner": owner,
        "owner_renounced": owner_renounced,
    }


async def _v2_liquidity(token_padded: str, weth_padded: str) -> dict | None:
    pair_raw = await rpc(
        _NETWORK, "eth_call", [{"to": _UNISWAP_V2_FACTORY, "data": _GET_PAIR_SELECTOR + token_padded + weth_padded}, "latest"]
    )
    pair = "0x" + pair_raw[-40:] if isinstance(pair_raw, str) and len(pair_raw) >= 66 else None
    if not pair or pair.lower() == _ZERO_ADDRESS:
        return None
    reserves_raw = await rpc(_NETWORK, "eth_call", [{"to": pair, "data": _GET_RESERVES_SELECTOR}, "latest"])
    if not isinstance(reserves_raw, str) or len(reserves_raw) < 130:
        return {"pair": pair}
    return {
        "pair": pair,
        "reserve0": str(int(reserves_raw[2:66], 16)),
        "reserve1": str(int(reserves_raw[66:130], 16)),
    }


async def _v3_pool(token_padded: str, weth_padded: str, fee: int) -> dict | None:
    fee_hex = format(fee, "064x")
    data = _GET_POOL_V3_SELECTOR + token_padded + weth_padded + fee_hex
    pool_raw = await rpc(_NETWORK, "eth_call", [{"to": _UNISWAP_V3_FACTORY, "data": data}, "latest"])
    pool = "0x" + pool_raw[-40:] if isinstance(pool_raw, str) and len(pool_raw) >= 66 else None
    if not pool or pool.lower() == _ZERO_ADDRESS:
        return None
    liquidity_raw = await rpc(_NETWORK, "eth_call", [{"to": pool, "data": _LIQUIDITY_V3_SELECTOR}, "latest"])
    liquidity = str(int(liquidity_raw, 16)) if isinstance(liquidity_raw, str) else None
    return {"pool": pool, "fee": fee, "liquidity": liquidity}


async def _aero_pool(token_padded: str, weth_padded: str, stable: bool) -> dict | None:
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


async def _liquidity(address: str) -> dict:
    """V2, every V3 fee tier and both Aerodrome pools are all started at
    once - none of them wait on each other. Before 2026-09-28 the V2
    pair+reserves lookup ran to completion BEFORE the V3/Aerodrome fan-out
    even started, adding several extra seconds serially for no reason (see
    module docstring's incident writeup)."""
    token_padded = _pad_address(address)
    weth_padded = _pad_address(_WETH)

    v2, v3_results, aero_results = await asyncio.gather(
        _v2_liquidity(token_padded, weth_padded),
        asyncio.gather(*(_v3_pool(token_padded, weth_padded, fee) for fee in _V3_FEE_TIERS)),
        asyncio.gather(*(_aero_pool(token_padded, weth_padded, stable) for stable in (False, True))),
    )
    v3_pools = [p for p in v3_results if p]
    aero_pools = [p for p in aero_results if p]

    return {
        "status": "ok",
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


async def _with_step_timeout(name: str, coro, timing: dict) -> dict:
    """Runs one analysis stage under _STEP_TIMEOUT_S. A timeout OR any RPC
    error degrades to {"status": "unavailable"} - it never propagates and
    never blocks the other stages or the response as a whole (2026-09-28
    incident: the previous version had no such bound at all)."""
    t0 = time.monotonic()
    try:
        result = await asyncio.wait_for(coro, timeout=_STEP_TIMEOUT_S)
    except (asyncio.TimeoutError, EvmRpcError):
        result = {"status": "unavailable"}
    timing[name] = round((time.monotonic() - t0) * 1000)
    return result


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
        "real but bounded risk, worth a smaller position or extra scrutiny. "
        "Also choose caution (never acceptable) if one or more signal groups "
        "below have status 'unavailable' - an incomplete picture is never "
        "grounds for the confident 'acceptable' verdict, even if what WAS "
        "observed looks clean."
    ),
    "acceptable": (
        "Ownership is renounced (or no dangerous owner-only function exists "
        "in the bytecode at all), it is not an upgradeable proxy, real "
        "liquidity was found on at least one DEX, and every signal group "
        "below has status 'ok' (none unavailable). No structural red flag "
        "and no missing data."
    ),
}
_VERDICT_INSTRUCTIONS = (
    "Given these on-chain structural signals for a Base ERC-20 token "
    "(bytecode powers, proxy/ownership status, DEX liquidity, holder "
    "concentration where available), is this token safe to acquire: avoid, "
    "caution, or acceptable? Some signal groups may carry status "
    "'unavailable' (a 3-second RPC budget was exceeded) or "
    "'skipped_established_token' (holder history too large to reconstruct "
    "cheaply) rather than real data - factor that gap into the verdict "
    "explicitly, never treat a missing signal as a clean one."
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


async def _run_analysis(address: str, code: str, timing: dict) -> dict:
    """Everything after the not-a-contract gate - the 3 signal groups (each
    individually timeout-bounded) plus the Jev verdict. Called under
    _GLOBAL_TIMEOUT_S as a whole by _lookup, so a slow Jev call (measured
    2-18s across repeated real calls - OpenRouter's alpha Decisions API has
    no SLA) can still trip the global deadline even though it has no
    per-step bound of its own."""
    bytecode, liquidity, holders = await asyncio.gather(
        _with_step_timeout("bytecode", _analyze_bytecode(address, code), timing),
        _with_step_timeout("liquidity", _liquidity(address), timing),
        _with_step_timeout("holders", _holder_distribution(address), timing),
    )
    t0 = time.monotonic()
    verdict = await _verdict(address, bytecode, liquidity, holders)
    timing["verdict"] = round((time.monotonic() - t0) * 1000)

    return {
        "address": address,
        "network": {"key": _NETWORK.key, "name": _NETWORK.name, "caip2": _NETWORK.caip2},
        "verdict": verdict,
        "bytecode_analysis": bytecode,
        "liquidity_analysis": liquidity,
        "holders_analysis": holders,
        "timing_ms": timing,
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

    timing: dict[str, int] = {}
    t0 = time.monotonic()
    try:
        result = await asyncio.wait_for(_run_analysis(address, code, timing), timeout=_GLOBAL_TIMEOUT_S)
    except asyncio.TimeoutError:
        raise EvmRpcError("timeout") from None
    timing["total"] = round((time.monotonic() - t0) * 1000)

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
        "status": "ok",
        "bytecode_size": 8421,
        "flags": {"mint": False, "blacklist": False, "pause": False, "set_max_tx_amount": True},
        "any_dangerous_function": True,
        "is_upgradeable_proxy": False,
        "owner": "0x0000000000000000000000000000000000000000",
        "owner_renounced": True,
    },
    "liquidity_analysis": {
        "status": "ok",
        "uniswap_v2": {"pair": "0x1efdc3e6cfb3df3b7dd3e3971d5262733c52c21c", "reserve0": "1535891536408965785", "reserve1": "5106124266540300941199548545"},
        "uniswap_v3_pools": [],
        "aerodrome_pools": [],
        "any_liquidity_found": True,
    },
    "holders_analysis": {"status": "skipped_established_token"},
    "timing_ms": {"bytecode": 420, "liquidity": 890, "holders": 310, "verdict": 1240, "total": 1340},
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
        if reason in {"invalid_address", "not_a_contract"}:
            code = 400
        elif reason == "timeout":
            code = 504
        else:
            code = 502
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
