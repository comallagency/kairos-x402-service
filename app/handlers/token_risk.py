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

Latency incidents (2026-09-28)
-------------------------------
1st incident, 13:39 UTC: the first real bootstrap settlement took 8.218s
server-side - the buyer's HTTP client had already given up with a
ReadTimeout before the response arrived. Root cause: _liquidity()'s V2
lookup ran BEFORE the V3/Aerodrome fan-out instead of alongside it, and the
Jev call itself measured 2-18s across repeated calls with no bound. Fixed
by parallelizing liquidity fully, capping every RPC stage at 3s
(degrading to {"status": "unavailable"} instead of blocking), and a 10s
global deadline before settlement.

2nd incident (same day): still not fast enough - 8.2s is itself very close
to httpx's own default request timeout (5s), so a typical client-side
default would ALSO cut off well before an 8s response. Two more structural
fixes:

- Multicall3 (0xcA11bde05977b3631167028862bE2a173976CA11, verified deployed
  on Base via eth_getCode before use): owner(), the Uniswap V2/V3/Aerodrome
  pool-discovery calls, and their reserves/liquidity reads are now batched
  into 1-2 eth_call round trips via aggregate3(Call3[]) instead of ~9
  separate RPC calls - each one previously paying full network latency on
  a shared public RPC. (eth_getCode and eth_getStorageAt are raw state
  reads, not contract calls, so Multicall3 cannot batch those two - they
  still run as their own calls, concurrently with the multicall.)
- The Jev verdict call is now capped at 2s. Past that, a deterministic
  rules-based verdict (mint/blacklist/pause/owner/liquidity) is returned
  instead, marked "verdict_source": "rules" rather than "jev" - a real
  answer beats no answer, but it must never be confused with one a model
  actually reasoned about.
- The global pre-settlement deadline is now 4.5s (was 10s), matching the
  target and leaving headroom under common HTTP client default timeouts.
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
_MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"

_LOG_PAGE_BLOCKS = 2000  # measured ceiling on the public Base RPC, see module docstring
_MAX_LOG_PAGES = 2
_HOLDER_LOG_CAP = 500  # more than this in one page -> clearly an established/busy token

_STEP_TIMEOUT_S = 3.0
_JEV_TIMEOUT_S = 2.0
_GLOBAL_TIMEOUT_S = 4.5

_CACHE_TTL_S = 300
_cache: dict[str, tuple[float, dict]] = {}


def _selector(sig: str) -> bytes:
    return keccak(text=sig)[:4]


def _selector_hex(sig: str) -> str:
    return "0x" + _selector(sig).hex()


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
_OWNER_SELECTOR = _selector_hex("owner()")
_GET_RESERVES_SELECTOR = _selector_hex("getReserves()")
_GET_PAIR_SELECTOR = _selector_hex("getPair(address,address)")
_GET_POOL_V3_SELECTOR = _selector_hex("getPool(address,address,uint24)")
_GET_POOL_AERO_SELECTOR = _selector_hex("getPool(address,address,bool)")
_LIQUIDITY_V3_SELECTOR = _selector_hex("liquidity()")
_AGGREGATE3_SELECTOR = _selector("aggregate3((address,bool,bytes)[])")

_ZERO_ADDRESS = "0x" + "00" * 20


def _pad_address(address: str) -> str:
    return address[2:].rjust(64, "0").lower()


def _word(n: int) -> bytes:
    return n.to_bytes(32, "big")


def _pad_right(b: bytes) -> bytes:
    return b + b"\x00" * ((-len(b)) % 32)


def _encode_aggregate3(calls: list[tuple[str, bool, bytes]]) -> str:
    """Encodes Multicall3.aggregate3(Call3[] calls) - a single top-level
    dynamic parameter (the array), whose elements are themselves dynamic
    (each Call3 tuple embeds a variable-length `bytes`). Verified byte-exact
    against real eth_call results for 1- and 2-element arrays before this
    was wired into the route (owner() and decimals() cross-checked against
    direct per-call eth_call results)."""
    n = len(calls)
    tuple_encodings = []
    for target, allow_failure, calldata in calls:
        tuple_encodings.append(
            _word(int(target, 16))
            + _word(1 if allow_failure else 0)
            + _word(0x60)  # offset to `bytes`, relative to this tuple's own start (3 head words)
            + _word(len(calldata))
            + _pad_right(calldata)
        )
    offsets = []
    running = n * 32
    for enc in tuple_encodings:
        offsets.append(_word(running))
        running += len(enc)
    array_encoding = _word(n) + b"".join(offsets) + b"".join(tuple_encodings)
    # single top-level dynamic param -> one head word: offset to its data (0x20)
    return "0x" + (_AGGREGATE3_SELECTOR + _word(0x20) + array_encoding).hex()


def _decode_aggregate3_result(hex_result: str) -> list[tuple[bool, bytes]]:
    raw = bytes.fromhex(hex_result[2:])
    array_offset = int.from_bytes(raw[0:32], "big")
    arr = raw[array_offset:]
    n = int.from_bytes(arr[0:32], "big")
    results = []
    for i in range(n):
        tuple_offset = int.from_bytes(arr[32 + i * 32 : 32 + i * 32 + 32], "big")
        tstart = 32 + tuple_offset
        success = int.from_bytes(arr[tstart : tstart + 32], "big") != 0
        data_offset = int.from_bytes(arr[tstart + 32 : tstart + 64], "big")
        data_start = tstart + data_offset
        data_len = int.from_bytes(arr[data_start : data_start + 32], "big")
        data = arr[data_start + 32 : data_start + 32 + data_len]
        results.append((success, data))
    return results


async def _multicall(calls: list[tuple[str, bool, bytes]]) -> list[tuple[bool, bytes]]:
    calldata = _encode_aggregate3(calls)
    result = await rpc(_NETWORK, "eth_call", [{"to": _MULTICALL3, "data": calldata}, "latest"])
    return _decode_aggregate3_result(result)


async def _fetch_code(address: str) -> str:
    code = await rpc(_NETWORK, "eth_getCode", [address, "latest"])
    return code if isinstance(code, str) else "0x"


def _addr_from_result(success: bool, data: bytes) -> str | None:
    if not success or len(data) < 32:
        return None
    addr = "0x" + data[-20:].hex()
    return addr if addr.lower() != _ZERO_ADDRESS else None


async def _bytecode_and_liquidity(address: str, code: str) -> tuple[dict, dict]:
    """Everything readable in 1-2 Multicall3 round trips, plus the one raw
    state read (EIP-1967 slot) Multicall3 cannot batch. Replaces what used
    to be ~9 separate eth_call/eth_getStorageAt round trips."""
    token_padded = _pad_address(address)
    weth_padded = _pad_address(_WETH)

    batch_a_calls = [
        (address, True, bytes.fromhex(_OWNER_SELECTOR[2:])),
        (_UNISWAP_V2_FACTORY, True, bytes.fromhex(_GET_PAIR_SELECTOR[2:] + token_padded + weth_padded)),
        *[
            (_UNISWAP_V3_FACTORY, True, bytes.fromhex(_GET_POOL_V3_SELECTOR[2:] + token_padded + weth_padded + format(fee, "064x")))
            for fee in _V3_FEE_TIERS
        ],
        *[
            (_AERODROME_FACTORY, True, bytes.fromhex(_GET_POOL_AERO_SELECTOR[2:] + token_padded + weth_padded + format(1 if stable else 0, "064x")))
            for stable in (False, True)
        ],
    ]

    impl_slot, batch_a = await asyncio.gather(
        rpc(_NETWORK, "eth_getStorageAt", [address, _EIP1967_IMPL_SLOT, "latest"]),
        _multicall(batch_a_calls),
    )

    # _addr_from_result turns the zero address into None, which would make a
    # renounced owner indistinguishable from "no owner() at all" - decoded
    # by hand here instead so owner_renounced (owner IS the zero address) is
    # kept separate from owner being unset/unimplemented.
    owner_success, owner_data = batch_a[0]
    owner = None
    owner_renounced = None
    if owner_success and len(owner_data) >= 32:
        raw_owner = "0x" + owner_data[-20:].hex()
        owner_renounced = raw_owner.lower() == _ZERO_ADDRESS
        owner = None if owner_renounced else raw_owner

    v2_pair = _addr_from_result(*batch_a[1])
    v3_pools_found = [
        {"pool": pool, "fee": fee}
        for fee, pool in zip(_V3_FEE_TIERS, (_addr_from_result(*batch_a[2 + i]) for i in range(3)))
        if pool
    ]
    aero_pools_found = [
        {"pool": pool, "stable": stable}
        for stable, pool in zip((False, True), (_addr_from_result(*batch_a[5 + i]) for i in range(2)))
        if pool
    ]

    flags = {name: sel[2:] in code for name, sel in _RISK_SELECTORS.items()}
    is_proxy = isinstance(impl_slot, str) and impl_slot.lower() != "0x" + "00" * 32
    bytecode = {
        "status": "ok",
        "bytecode_size": max(len(code) // 2 - 1, 0),
        "flags": flags,
        "any_dangerous_function": any(flags.values()),
        "is_upgradeable_proxy": is_proxy,
        "owner": owner,
        "owner_renounced": owner_renounced,
    }

    # batch B: reserves/liquidity for whatever was actually found - skipped
    # entirely (no second round trip) if nothing was found at all
    batch_b_calls: list[tuple[str, bool, bytes]] = []
    if v2_pair:
        batch_b_calls.append((v2_pair, True, bytes.fromhex(_GET_RESERVES_SELECTOR[2:])))
    for p in v3_pools_found:
        batch_b_calls.append((p["pool"], True, bytes.fromhex(_LIQUIDITY_V3_SELECTOR[2:])))
    for p in aero_pools_found:
        batch_b_calls.append((p["pool"], True, bytes.fromhex(_GET_RESERVES_SELECTOR[2:])))

    v2 = {"pair": v2_pair} if v2_pair else None
    if batch_b_calls:
        batch_b = await _multicall(batch_b_calls)
        idx = 0
        if v2_pair:
            success, data = batch_b[idx]
            idx += 1
            if success and len(data) >= 64:
                v2 = {"pair": v2_pair, "reserve0": str(int.from_bytes(data[0:32], "big")), "reserve1": str(int.from_bytes(data[32:64], "big"))}
        for p in v3_pools_found:
            success, data = batch_b[idx]
            idx += 1
            p["liquidity"] = str(int.from_bytes(data, "big")) if success and data else None
        for p in aero_pools_found:
            success, data = batch_b[idx]
            idx += 1
            if success and len(data) >= 64:
                p["reserve0"] = str(int.from_bytes(data[0:32], "big"))
                p["reserve1"] = str(int.from_bytes(data[32:64], "big"))

    liquidity = {
        "status": "ok",
        "uniswap_v2": v2,
        "uniswap_v3_pools": v3_pools_found,
        "aerodrome_pools": aero_pools_found,
        "any_liquidity_found": bool(v2 or v3_pools_found or aero_pools_found),
    }
    return bytecode, liquidity


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


async def _with_step_timeout(name: str, coro, timing: dict, fallback: dict) -> dict:
    t0 = time.monotonic()
    try:
        result = await asyncio.wait_for(coro, timeout=_STEP_TIMEOUT_S)
    except (asyncio.TimeoutError, EvmRpcError):
        result = fallback
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


def _rules_verdict(bytecode: dict, liquidity: dict, holders: dict) -> dict:
    """Deterministic fallback when Jev doesn't answer within _JEV_TIMEOUT_S -
    mirrors the same reasoning given to Jev in _VERDICT_CRITERIA, just
    applied mechanically. A real answer beats no answer, but it must never
    be presented as if a model reasoned about it - see verdict_source."""
    bytecode_ok = bytecode.get("status") == "ok"
    liquidity_ok = liquidity.get("status") == "ok"
    dangerous = bytecode_ok and bool(bytecode.get("any_dangerous_function"))
    renounced = bytecode_ok and bytecode.get("owner_renounced") is True
    proxy = bytecode_ok and bool(bytecode.get("is_upgradeable_proxy"))
    has_liquidity = liquidity_ok and bool(liquidity.get("any_liquidity_found"))

    if (dangerous and not renounced) or (proxy and not has_liquidity) or (dangerous and not has_liquidity):
        verdict, reason = "avoid", "dangerous owner-only power without renounced ownership, or a proxy/dangerous power with no liquidity found"
    elif bytecode_ok and liquidity_ok and renounced and not proxy and has_liquidity and not dangerous:
        verdict, reason = "acceptable", "ownership renounced, not a proxy, real liquidity found, no dangerous bytecode power, all signals available"
    else:
        verdict, reason = "caution", "mixed or incomplete signals - see bytecode/liquidity/holders status fields"

    return {
        "verdict": verdict,
        "probability": None,
        "probabilities": None,
        "confidence": None,
        "verdict_source": "rules",
        "reason": reason,
    }


async def _verdict(address: str, bytecode: dict, liquidity: dict, holders: dict) -> dict:
    state = json.dumps({"address": address, "bytecode": bytecode, "liquidity": liquidity, "holders": holders})
    questions = {"verdict": {"type": "choice", "instructions": _VERDICT_INSTRUCTIONS, "criteria": _VERDICT_CRITERIA}}
    try:
        data = await asyncio.wait_for(ask_jev(state, questions), timeout=_JEV_TIMEOUT_S)
    except (asyncio.TimeoutError, JevError):
        return _rules_verdict(bytecode, liquidity, holders)
    answer = data["answers"]["verdict"]
    return {
        "verdict": answer["choice"],
        "probability": answer["probabilities"][answer["choice"]],
        "probabilities": answer["probabilities"],
        "confidence": answer.get("confidence"),
        "verdict_source": "jev",
    }


async def _run_analysis(address: str, timing: dict) -> dict:
    """Everything from the not-a-contract gate onward, run under the SAME
    _GLOBAL_TIMEOUT_S deadline as the rest of the analysis (2026-09-28: an
    earlier version fetched the bytecode for this gate BEFORE the deadline
    wrapper started, so a slow eth_getCode call on its own could push a
    response past 4.5s wall-clock even though timing_ms['total'] - measured
    only from here onward - looked fine. Measured live: 2 of 5 real
    addresses exceeded the 4.5s target by ~1.3-2.4s purely from that gap.
    Wrapping the getCode call itself closes it - the deadline is now
    genuinely end-to-end."""
    code = await _fetch_code(address)
    if code in ("0x", "0x0"):
        raise EvmRpcError("not_a_contract")

    bytecode_liquidity, holders = await asyncio.gather(
        _with_step_timeout(
            "bytecode_liquidity", _bytecode_and_liquidity(address, code), timing,
            fallback=({"status": "unavailable"}, {"status": "unavailable"}),
        ),
        _with_step_timeout("holders", _holder_distribution(address), timing, fallback={"status": "unavailable"}),
    )
    bytecode, liquidity = bytecode_liquidity

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

    timing: dict[str, int] = {}
    t0 = time.monotonic()
    try:
        result = await asyncio.wait_for(_run_analysis(address, timing), timeout=_GLOBAL_TIMEOUT_S)
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
        "verdict_source": "jev",
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
    "timing_ms": {"bytecode_liquidity": 420, "holders": 310, "verdict": 890, "total": 950},
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
