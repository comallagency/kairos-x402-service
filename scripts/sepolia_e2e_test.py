#!/usr/bin/env python3
"""One-off manual test (not part of the app): pay a real x402 challenge on Base
Sepolia end-to-end against the locally running server, using a throwaway local
wallet funded via the CDP testnet faucet. Run with the server already up on
127.0.0.1:8091.
"""
import asyncio
import json
import sys
import time

import httpx
from eth_account import Account

from app import config

import os

SERVER = os.getenv("E2E_SERVER", "http://127.0.0.1:8091")


async def main() -> None:
    from cdp import CdpClient
    from x402 import x402Client
    from x402.mechanisms.evm.exact.register import register_exact_evm_client

    account = Account.create()
    print(f"test buyer wallet: {account.address}")

    cdp = CdpClient(api_key_id=config.CDP_API_KEY_ID, api_key_secret=config.CDP_API_KEY_SECRET)
    tx_hash = await cdp.evm.request_faucet(address=account.address, network="base-sepolia", token="usdc")
    print(f"faucet tx: {tx_hash} - waiting for it to land...")
    await cdp.close()

    usdc_address = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
    balance = 0
    for attempt in range(20):
        await asyncio.sleep(3)
        async with httpx.AsyncClient(timeout=15.0) as client:
            selector = "0x70a08231"  # balanceOf(address)
            data = selector + account.address[2:].rjust(64, "0").lower()
            resp = await client.post(
                "https://sepolia.base.org",
                json={
                    "jsonrpc": "2.0", "id": 1, "method": "eth_call",
                    "params": [{"to": usdc_address, "data": data}, "latest"],
                },
            )
            result = resp.json().get("result", "0x0")
            balance = int(result, 16)
        print(f"  attempt {attempt+1}: balance = {balance / 1e6} USDC")
        if balance > 0:
            break

    if balance == 0:
        print("faucet funds never arrived, aborting", file=sys.stderr)
        sys.exit(1)

    x402client = x402Client()
    register_exact_evm_client(x402client, signer=account, networks="eip155:84532")

    from x402.http.clients.httpx import x402HttpxClient

    async with x402HttpxClient(x402client, base_url=SERVER, timeout=30.0) as client:
        resp = await client.post("/search", json={"query": "best ramen restaurants in Shibuya Tokyo"})
        print(f"status: {resp.status_code}")
        print(json.dumps(resp.json(), indent=2)[:2000])


if __name__ == "__main__":
    asyncio.run(main())
