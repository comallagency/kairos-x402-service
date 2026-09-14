#!/usr/bin/env python3
"""One-off manual test (not part of the app): proves the "first call free per
wallet" feature end-to-end on Base Sepolia against the locally running server.

Same call twice, same throwaway wallet, funded via the CDP testnet faucet:
  1st /search call -> expect price_paid_usdc == 0.0, no real settlement (the
     x402ResourceServer.on_before_settle hook in app/x402_setup.py skips the
     facilitator settle call - verify still ran normally, so signature/funds
     were genuinely checked).
  2nd /search call, same wallet -> expect price_paid_usdc == 0.10, real
     settlement (USDC actually moves).

Run with the server already up on 127.0.0.1:8091.
"""
import asyncio
import json
import os
import sys

import httpx
from eth_account import Account

from app import config

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
        print("\n--- call 1 (expect free) ---")
        resp1 = await client.post("/search", json={"query": "best ramen restaurants in Shibuya Tokyo"})
        print(f"status: {resp1.status_code}")
        body1 = resp1.json()
        print(json.dumps(body1, indent=2)[:1500])
        receipt1 = body1.get("x402_receipt", {})
        settle_header1 = resp1.headers.get("x-payment-response")
        print(f"price_paid_usdc: {receipt1.get('price_paid_usdc')}")
        print(f"x-payment-response header: {settle_header1}")

        print("\n--- call 2, same wallet (expect normal price) ---")
        resp2 = await client.post("/search", json={"query": "best ramen restaurants in Shibuya Tokyo"})
        print(f"status: {resp2.status_code}")
        body2 = resp2.json()
        print(json.dumps(body2, indent=2)[:1500])
        receipt2 = body2.get("x402_receipt", {})
        settle_header2 = resp2.headers.get("x-payment-response")
        print(f"price_paid_usdc: {receipt2.get('price_paid_usdc')}")
        print(f"x-payment-response header: {settle_header2}")

    assert receipt1.get("price_paid_usdc") == 0.0, "1st call should be free"
    assert receipt2.get("price_paid_usdc") == 0.10, "2nd call should be charged normally"
    print("\nPASS: first call free per wallet, second call charged normally.")


if __name__ == "__main__":
    asyncio.run(main())
