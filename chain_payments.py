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
import asyncio
import sys
from datetime import datetime, timezone

import httpx
from x402.mechanisms.evm.default_assets import get_default_asset

from app import config, db

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

RPC_URLS = {
    "eip155:8453": "https://mainnet.base.org",
    "eip155:84532": "https://sepolia.base.org",
}

# First run only (no chain_sync_state yet): how far back to backfill.
INITIAL_BACKFILL_BLOCKS = 200_000

# eth_getLogs providers cap the block range per call - stay conservative.
BLOCK_CHUNK = 5_000


def _mechanical_wallets() -> set[str]:
    raw = getattr(config, "MECHANICAL_WALLETS", "") or ""
    return {w.strip().lower() for w in raw.split(",") if w.strip()}


async def _rpc_call(client: httpx.AsyncClient, url: str, method: str, params: list):
    resp = await client.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"RPC error calling {method}: {data['error']}")
    return data["result"]


def _pad_address_topic(address: str) -> str:
    return "0x" + address.lower().removeprefix("0x").rjust(64, "0")


async def sync() -> None:
    network = config.X402_NETWORK
    rpc_url = RPC_URLS.get(network)
    if not rpc_url:
        print(f"no RPC endpoint configured for network {network}", file=sys.stderr)
        return
    if config.X402_PAY_TO == "0x0000000000000000000000000000000000000000":
        print("X402_PAY_TO is still the placeholder address, skipping chain sync", file=sys.stderr)
        return

    asset = get_default_asset(network)
    usdc_address = asset["asset"]
    decimals = asset["decimals"]
    mechanical = _mechanical_wallets()

    async with httpx.AsyncClient(timeout=30.0) as client:
        latest_block = int(await _rpc_call(client, rpc_url, "eth_blockNumber", []), 16)

        last_synced = db.get_chain_sync_state(f"last_block:{network}")
        from_block = int(last_synced) + 1 if last_synced else max(0, latest_block - INITIAL_BACKFILL_BLOCKS)

        if from_block > latest_block:
            print("nothing new to sync")
            return

        block_time_cache: dict[int, str] = {}
        synced = 0
        current = from_block
        while current <= latest_block:
            end = min(current + BLOCK_CHUNK - 1, latest_block)
            logs = await _rpc_call(
                client,
                rpc_url,
                "eth_getLogs",
                [
                    {
                        "fromBlock": hex(current),
                        "toBlock": hex(end),
                        "address": usdc_address,
                        "topics": [TRANSFER_TOPIC, None, _pad_address_topic(config.X402_PAY_TO)],
                    }
                ],
            )
            for log in logs:
                tx_hash = log["transactionHash"]
                from_addr = "0x" + log["topics"][1][-40:]
                amount = int(log["data"], 16) / (10**decimals)
                block_num = int(log["blockNumber"], 16)
                if block_num not in block_time_cache:
                    block = await _rpc_call(client, rpc_url, "eth_getBlockByNumber", [hex(block_num), False])
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
            current = end + 1

        db.set_chain_sync_state(f"last_block:{network}", str(latest_block))
        print(f"synced {synced} new USDC transfer(s) to {config.X402_PAY_TO} on {network}, up to block {latest_block}")


if __name__ == "__main__":
    asyncio.run(sync())
