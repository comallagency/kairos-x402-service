"""Shared RPC transport for the Base read-only pack (app/base_chain/routes/*.py).

Wraps app.upstream.evm_rpc.rpc() - the same multi-provider, auto-failover
client already used by /wallet-balance, /gas-price and /token-risk - with
a hard 3-second ceiling specific to this pack (RpcTimeout), so a slow/dead
provider can never make a buyer wait past the point where we'd rather 504
for free than settle a payment for a response we didn't deliver in time.
evm_rpc.rpc() itself is untouched: its own 12s-per-provider timeout still
applies to those other routes, which have their own latency budgets.

Multicall3 aggregate3 encode/decode is the same byte-exact algorithm
verified in app/handlers/token_risk.py (checked there against real eth_call
results for owner()/decimals() before being trusted) - reproduced here as
a public, shared utility instead of importing a token-risk-private helper,
since this pack has nothing else to do with token-risk's module.
"""

from __future__ import annotations

import asyncio
import time

import httpx

from app.upstream.evm_rpc import NETWORKS

BASE = NETWORKS["base"]
RPC_TIMEOUT_S = 3.0
_MIN_PER_PROVIDER_S = 1.0
# canonical Multicall3 deployment address, identical on every EVM chain
MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"
_AGGREGATE3_SELECTOR = bytes.fromhex("82ad56cb")  # aggregate3(Call3[])


class RpcTimeout(Exception):
    """The upstream RPC did not answer within RPC_TIMEOUT_S. Never settled -
    the route handler must turn this into a free 504, not a 422 or 5xx."""


class RpcApplicationError(Exception):
    """The upstream node returned a well-formed JSON-RPC error that is
    deterministic given the query and the chain's current state - EVM
    execution reverted (code 3) - not a transient provider fault. Raised
    immediately by call() instead of retrying the other providers (an
    identical call reverts on any full node tracking the same chain state)
    and burning the whole RPC_TIMEOUT_S budget on a retry that cannot
    succeed differently. Route handlers turn this into a free 422.

    Deliberately NOT extended to response-too-large / block-range-too-wide
    errors (-32020, -32614, -32602, etc.): those are per-provider operator
    policy, not chain truth - measured directly (2026-10-03) that
    base.drpc.org (tried first) caps eth_getLogs at 50 blocks while
    mainnet.base.org allows 2,000 for the exact same query. Fail-fasting on
    the first provider's policy would defeat the "plusieurs fournisseurs en
    secours, bascule automatique" requirement outright - the whole point of
    a second provider is that it may succeed where the first's policy
    refused. These stay on the normal per-provider retry path; if every
    provider's limit is tighter than the request within RPC_TIMEOUT_S, the
    loop exhausts into the ordinary RpcTimeout -> free 504, which is exactly
    the pack's own "can't deliver in time, don't charge" contract - no
    special-casing needed."""

    def __init__(self, code, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def _is_fail_fast_error(err: dict) -> bool:
    return err.get("code") == 3  # EVM execution reverted


# Same provider set as app.upstream.evm_rpc.NETWORKS["base"] (the shared
# client already used by /wallet-balance, /gas-price, /token-risk) -
# reordered, not replaced. Measured directly (2026-10-03): base.drpc.org
# and mainnet.base.org both answer every method in 100-200ms; 1rpc.io/base
# ranged 300ms-1.7s and returned a bare 410 Gone for eth_getCode. evm_rpc.rpc()
# gives every provider its own 12s timeout before falling back - fine for
# routes with no hard SLA, but this pack's 3s *total* ceiling means trying
# the unreliable one first could burn the whole budget before the fast ones
# ever get a turn. Own short-timeout, fail-fast loop instead, so a dead
# first provider still leaves time for the other two inside RPC_TIMEOUT_S.
_PROVIDER_ORDER = (
    "https://base.drpc.org",
    "https://mainnet.base.org",
    "https://1rpc.io/base",
)

# One shared, long-lived client per event loop instead of opening a fresh
# connection (TCP + TLS handshake) on every single RPC call - measured
# directly (2026-10-03) against base.drpc.org: a new httpx.AsyncClient() per
# call averaged ~270ms with spikes to 430ms+, a reused client with
# connection pooling settled to ~95-120ms after the first request. This was
# the main reason several batch-2 routes (live-balance, total-supply,
# erc20-transfers) initially missed the pack's 500ms p95 target.
#
# Keyed by the running event loop (not just created once) because an
# httpx.AsyncClient's connections are bound to the loop that opened them -
# production has exactly one loop for the process lifetime, so this is a
# create-once client there, but pytest-asyncio gives each test function its
# own loop, and reusing a client across loops raises "attached to a
# different loop" - recreating it when the loop changes makes this correct
# in both environments.
_client: httpx.AsyncClient | None = None
_client_loop: asyncio.AbstractEventLoop | None = None


async def _get_client() -> httpx.AsyncClient:
    global _client, _client_loop
    loop = asyncio.get_running_loop()
    if _client is None or _client_loop is not loop:
        if _client is not None:
            # closing the stale client (bound to a now-dead loop, e.g. the
            # previous pytest-asyncio test) matters, not just hygiene: left
            # unclosed, its open connections/file descriptors accumulate
            # across every loop switch - with pytest giving each test its
            # own loop, a full suite run leaks one per test. Found this by
            # reproducing a rare, otherwise-inexplicable RpcTimeout with an
            # empty last_error (2026-10-03) only under the full batch1+2
            # suite (91 tests), never in small isolated runs - consistent
            # with resource exhaustion building up over the run, not a
            # per-call bug.
            try:
                await _client.aclose()
            except Exception:
                pass
        # retries=1 at the transport level absorbs a stale pooled keep-alive
        # connection transparently (the server closing an idle connection
        # right as we reuse it) - a handful of otherwise-inexplicable
        # RpcTimeouts with an empty last_error surfaced only after this
        # module started reusing a client (2026-10-03), consistent with
        # that exact class of error; httpx's built-in retry is the standard
        # fix rather than teaching our own provider-fallback loop about it.
        _client = httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(retries=1))
        _client_loop = loop
    return _client


async def call(method: str, params: list):
    """One JSON-RPC call against Base, trying each provider in
    _PROVIDER_ORDER with its own short timeout, bounded overall by
    RPC_TIMEOUT_S regardless of how many providers it tries."""
    deadline = time.monotonic() + RPC_TIMEOUT_S
    last_error: Exception | None = None
    client = await _get_client()
    for i, url in enumerate(_PROVIDER_ORDER):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        providers_left = len(_PROVIDER_ORDER) - i
        per_call_timeout = min(remaining, max(_MIN_PER_PROVIDER_S, remaining / providers_left))
        try:
            response = await client.post(
                url,
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                timeout=per_call_timeout,
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("error"):
                err = payload["error"]
                if _is_fail_fast_error(err):
                    raise RpcApplicationError(err.get("code"), err.get("message", "rpc error"))
                last_error = RuntimeError(f"rpc_error: {err}")
                continue
            if "result" not in payload:
                last_error = RuntimeError("rpc_invalid_response")
                continue
            return payload["result"]
        except (httpx.HTTPError, ValueError) as exc:
            last_error = exc
            continue
    raise RpcTimeout(f"rpc_unavailable: {last_error}")


async def gather(*coros):
    """Run independent RPC calls concurrently under one shared RPC_TIMEOUT_S
    ceiling (not one timeout per call) - used for routes that group several
    JSON-RPC methods that Multicall3 can't batch (it only batches eth_call
    contract calls, not arbitrary methods like eth_blockNumber/eth_gasPrice)."""
    try:
        return await asyncio.wait_for(asyncio.gather(*coros), timeout=RPC_TIMEOUT_S)
    except asyncio.TimeoutError as exc:
        raise RpcTimeout("rpc_timeout") from exc


def _word(n: int) -> bytes:
    return n.to_bytes(32, "big")


def _pad_right(b: bytes) -> bytes:
    return b + b"\x00" * ((-len(b)) % 32)


def _encode_aggregate3(calls: list[tuple[str, bool, bytes]]) -> str:
    """Encodes Multicall3.aggregate3(Call3[] calls). See module docstring -
    same algorithm as token_risk.py's _encode_aggregate3, verified there
    byte-exact against real eth_call results."""
    n = len(calls)
    tuple_encodings = []
    for target, allow_failure, calldata in calls:
        tuple_encodings.append(
            _word(int(target, 16))
            + _word(1 if allow_failure else 0)
            + _word(0x60)
            + _word(len(calldata))
            + _pad_right(calldata)
        )
    offsets = []
    running = n * 32
    for enc in tuple_encodings:
        offsets.append(_word(running))
        running += len(enc)
    array_encoding = _word(n) + b"".join(offsets) + b"".join(tuple_encodings)
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


async def multicall(calls: list[tuple[str, bool, bytes]]) -> list[tuple[bool, bytes]]:
    """calls: list of (target_address, allow_failure, calldata). Returns
    list of (success, return_data) in the same order, in ONE round trip."""
    calldata = _encode_aggregate3(calls)
    result = await call("eth_call", [{"to": MULTICALL3, "data": calldata}, "latest"])
    return _decode_aggregate3_result(result)


def selector(signature: str) -> bytes:
    """keccak4(signature) - the 4-byte function selector."""
    from eth_utils import keccak

    return keccak(text=signature)[:4]


def encode_address_arg(address: str) -> bytes:
    return bytes(12) + bytes.fromhex(address[2:].lower().rjust(40, "0"))


def encode_uint256_arg(n: int) -> bytes:
    return _word(n)


def decode_uint256(data: bytes) -> int:
    if len(data) < 32:
        return 0
    return int.from_bytes(data[:32], "big")


def decode_address(data: bytes) -> str | None:
    if len(data) < 32:
        return None
    addr = "0x" + data[-20:].hex()
    return addr if int(addr, 16) != 0 else None


def is_valid_address(value: str) -> bool:
    if not isinstance(value, str) or not value.startswith("0x") or len(value) != 42:
        return False
    try:
        int(value, 16)
        return True
    except ValueError:
        return False


def is_valid_hash(value: str) -> bool:
    if not isinstance(value, str) or not value.startswith("0x") or len(value) != 66:
        return False
    try:
        int(value, 16)
        return True
    except ValueError:
        return False
