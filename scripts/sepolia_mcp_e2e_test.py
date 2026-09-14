#!/usr/bin/env python3
"""One-off manual test (not part of the app): pay a real x402 challenge on Base
Sepolia end-to-end through the MCP `search` tool (not the HTTP route), using a
throwaway local wallet funded via the CDP testnet faucet - same approach as
scripts/sepolia_e2e_test.py, adapted to call the tool via fastmcp.Client instead
of an HTTP client. Run with the server already up on 127.0.0.1:8091 (real .env,
i.e. real CDP_API_KEY_ID/SECRET + X402_NETWORK=eip155:84532 - both already the
local defaults in this project's .env, no override needed).
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
    from fastmcp import Client
    from x402 import x402Client
    from x402.mechanisms.evm.exact.register import register_exact_evm_client
    from x402.schemas import parse_payment_required

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

    async with Client(f"{SERVER}/mcp/") as mcp_client:
        print("\n=== step 1: call search WITHOUT payment (discover price) ===")
        unpaid = await mcp_client.call_tool(
            "search", {"query": "best ramen restaurants in Shibuya Tokyo"}, raise_on_error=False
        )
        print("is_error:", unpaid.is_error)
        assert unpaid.is_error, "expected a payment-required error on the unpaid call"
        payment_required_dict = unpaid.structured_content
        print("accepts:", payment_required_dict["accepts"])

        payment_required = parse_payment_required(payment_required_dict)
        extensions = payment_required_dict.get("extensions")

        print("\n=== step 2: sign a real Sepolia payment for the quoted price ===")
        payment_payload = await x402client.create_payment_payload(
            payment_required, resource=None, extensions=extensions
        )
        payment_meta = payment_payload.model_dump(by_alias=True, exclude_none=True)
        print("payment payload (payer):", payment_meta.get("payload", {}).get("authorization", {}).get("from"))

        print("\n=== step 3: retry search WITH payment attached via _meta ===")
        paid = await mcp_client.call_tool(
            "search",
            {"query": "best ramen restaurants in Shibuya Tokyo"},
            meta={"x402/payment": payment_meta},
            raise_on_error=False,
        )
        print("is_error:", paid.is_error)
        print(json.dumps(paid.structured_content, indent=2)[:2000])
        print("meta:", json.dumps(paid.meta, indent=2)[:1000] if paid.meta else None)
        assert not paid.is_error, "expected the paid call to succeed"
        assert paid.structured_content.get("results"), "expected real search results in the response"

    print("\nALL REAL SEPOLIA MCP PAYMENT CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
