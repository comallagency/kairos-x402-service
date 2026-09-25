#!/usr/bin/env python3
"""Cron script (hourly): sync USDC transfers to X402_PAY_TO on Base, feed the dashboard.

    */60 * * * * cd /opt/x402/app && .venv/bin/python chain_payments.py >> logs/chain_payments.log 2>&1

"Mechanical" buyer tagging is a configurable allowlist (MECHANICAL_WALLETS, comma
-separated addresses in .env), NOT a live cross-vendor Bazaar query: there is no
API that answers "how many x402 vendors has this wallet paid" on demand. Comall's
own 2026-09-05 market research already identified which wallets behave this way
(~4 wallets driving 24% of ecosystem-wide micro-payments) - drop those addresses
into MECHANICAL_WALLETS once known for this specific payTo.
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone

import httpx
from x402.mechanisms.evm.default_assets import get_default_asset

from app import config, db

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

# Public Base RPC endpoints known to support eth_getLogs (probed 2026-09-19).
# mainnet.base.org returns HTTP 413 on wide USDC windows — fall through, then
# shrink the block window when every endpoint rejects the range.
_DEFAULT_RPC = {
    "eip155:8453": [
        "https://1rpc.io/base",
        "https://base.drpc.org",
        "https://mainnet.base.org",
        # BlastAPI hard-caps eth_getLogs at 10 blocks — last resort only.
        "https://base-mainnet.public.blastapi.io",
    ],
    "eip155:84532": [
        "https://sepolia.base.org",
    ],
}

# First run only (no chain_sync_state yet): ~1 day of Base (~2s/block).
INITIAL_BACKFILL_BLOCKS = int(os.getenv("CHAIN_BACKFILL_BLOCKS", "50000"))

# Prefer larger windows; shrink to MIN_CHUNK when every RPC rejects the range.
BLOCK_CHUNK = int(os.getenv("CHAIN_BLOCK_CHUNK", "100"))
MIN_CHUNK = 1


def _rpc_urls(network: str) -> list[str]:
    override = (os.getenv("BASE_RPC_URL") or os.getenv("CHAIN_RPC_URL") or "").strip()
    if override:
        # Still keep public fallbacks after the preferred URL.
        extras = [u for u in (_DEFAULT_RPC.get(network) or []) if u != override]
        return [override] + extras
    return list(_DEFAULT_RPC.get(network) or [])


def _mechanical_wallets() -> set[str]:
    raw = getattr(config, "MECHANICAL_WALLETS", "") or ""
    return {w.strip().lower() for w in raw.split(",") if w.strip()}


class RpcHttpError(RuntimeError):
    def __init__(self, status: int, body: str):
        self.status = status
        self.body = body
        super().__init__(f"RPC HTTP {status}: {body[:200]}")


def _is_range_too_large(msg: str) -> bool:
    low = msg.lower()
    return any(
        s in low
        for s in (
            "block range",
            "too large",
            "query returned more",
            "413",
            "limit exceeded",
            "response size",
            "log response size",
            "10 block",
            "block range should work",
        )
    )


async def _rpc_call(client: httpx.AsyncClient, url: str, method: str, params: list):
    resp = await client.post(
        url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    )
    body = resp.text
    if resp.status_code == 413:
        raise RpcHttpError(413, body)
    # Some providers return HTTP 400 with a JSON-RPC "block range" error.
    if resp.status_code >= 400:
        if _is_range_too_large(body):
            raise RpcHttpError(413, body)
        raise RpcHttpError(resp.status_code, body)
    try:
        data = resp.json()
    except ValueError as exc:
        raise RuntimeError(f"RPC non-JSON from {url}: {body[:120]}") from exc
    if "error" in data:
        err = data["error"]
        msg = err if isinstance(err, str) else str(err)
        if _is_range_too_large(msg):
            raise RpcHttpError(413, msg)
        raise RuntimeError(f"RPC error calling {method}: {err}")
    return data["result"]


async def _rpc_call_failover(
    client: httpx.AsyncClient, urls: list[str], method: str, params: list
):
    """Try each URL. For eth_getLogs, only surface 413 after every URL rejects the range
    so the caller can shrink the window; other errors fall through to the next URL.
    HTTP 429 is retried with short backoff on the same URL before moving on."""
    last: Exception | None = None
    saw_413 = False
    for url in urls:
        for attempt in range(3):
            try:
                return await _rpc_call(client, url, method, params), url
            except RpcHttpError as exc:
                last = exc
                if exc.status == 429:
                    await asyncio.sleep(0.4 * (attempt + 1))
                    continue
                if method == "eth_getLogs" and exc.status == 413:
                    saw_413 = True
                break  # try next URL
            except (httpx.HTTPError, RuntimeError, ValueError) as exc:
                last = exc
                break
        else:
            # exhausted 429 retries on this URL
            continue
    if method == "eth_getLogs" and saw_413:
        raise RpcHttpError(413, str(last) if last else "all RPCs rejected eth_getLogs range")
    assert last is not None
    raise last


def _pad_address_topic(address: str) -> str:
    return "0x" + address.lower().removeprefix("0x").rjust(64, "0")


async def sync() -> None:
    network = config.X402_NETWORK
    urls = _rpc_urls(network)
    if not urls:
        print(f"no RPC endpoint configured for network {network}", file=sys.stderr)
        return
    if config.X402_PAY_TO == "0x0000000000000000000000000000000000000000":
        print("X402_PAY_TO is still the placeholder address, skipping chain sync", file=sys.stderr)
        return

    asset = get_default_asset(network)
    usdc_address = asset["asset"]
    decimals = asset["decimals"]
    mechanical = _mechanical_wallets()
    pay_topic = _pad_address_topic(config.X402_PAY_TO)

    async with httpx.AsyncClient(timeout=45.0) as client:
        latest_block, active_url = await _rpc_call_failover(
            client, urls, "eth_blockNumber", []
        )
        latest_block = int(latest_block, 16)
        # Prefer the URL that answered eth_blockNumber for the rest of the run.
        prefer = [active_url] + [u for u in urls if u != active_url]

        last_synced = db.get_chain_sync_state(f"last_block:{network}")
        from_block = (
            int(last_synced) + 1
            if last_synced
            else max(0, latest_block - INITIAL_BACKFILL_BLOCKS)
        )

        if from_block > latest_block:
            print("nothing new to sync")
            return

        block_time_cache: dict[int, str] = {}
        synced = 0
        current = from_block
        chunk = BLOCK_CHUNK

        while current <= latest_block:
            end = min(current + chunk - 1, latest_block)
            try:
                logs, used = await _rpc_call_failover(
                    client,
                    prefer,
                    "eth_getLogs",
                    [
                        {
                            "fromBlock": hex(current),
                            "toBlock": hex(end),
                            "address": usdc_address,
                            "topics": [TRANSFER_TOPIC, None, pay_topic],
                        }
                    ],
                )
                prefer = [used] + [u for u in prefer if u != used]
            except RpcHttpError as exc:
                if exc.status == 413 and chunk > MIN_CHUNK:
                    chunk = max(MIN_CHUNK, chunk // 2)
                    print(
                        f"eth_getLogs too heavy [{current}-{end}], retry with chunk={chunk}",
                        file=sys.stderr,
                    )
                    continue
                raise

            for log in logs:
                tx_hash = log["transactionHash"]
                from_addr = "0x" + log["topics"][1][-40:]
                amount = int(log["data"], 16) / (10**decimals)
                block_num = int(log["blockNumber"], 16)
                if block_num not in block_time_cache:
                    block, _ = await _rpc_call_failover(
                        client, prefer, "eth_getBlockByNumber", [hex(block_num), False]
                    )
                    block_time_cache[block_num] = datetime.fromtimestamp(
                        int(block["timestamp"], 16), tz=timezone.utc
                    ).isoformat()
                db.upsert_chain_payment(
                    tx_hash=tx_hash,
                    from_address=from_addr,
                    amount_usdc=amount,
                    block_number=block_num,
                    block_time=block_time_cache[block_num],
                    is_mechanical=from_addr.lower() in mechanical,
                    network=network,
                )
                synced += 1

            # Advance cursor even if this window had 0 transfers — otherwise a
            # healthy empty window would be re-scanned forever.
            db.set_chain_sync_state(f"last_block:{network}", str(end))
            current = end + 1
            # Slowly grow the window back after a successful call.
            if chunk < BLOCK_CHUNK:
                chunk = min(BLOCK_CHUNK, chunk * 2)
            # Soft pacing so public RPCs do not 429 us mid catch-up.
            await asyncio.sleep(0.05)

        print(
            f"synced {synced} new USDC transfer(s) to {config.X402_PAY_TO} "
            f"on {network}, up to block {latest_block} via {prefer[0]}"
        )


if __name__ == "__main__":
    asyncio.run(sync())
