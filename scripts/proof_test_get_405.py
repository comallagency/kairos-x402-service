"""One-off diagnostic (operator-requested, 2026-10-09): does a real x402
payment actually SETTLE on-chain for a GET request to a route with no
FastAPI GET handler, even though the final HTTP response is a 404/405?

Needs KEY_A/KEY_B (the real signing keys) in the environment - never run
this from an automated session; the operator runs it themselves.

Status as of this commit: /text/slug (the TARGET_PATH below) already got
its own real GET handler in the same-day fix, so running this now proves
the fix delivers rather than reproducing the original bug - a read-only
chain_payments-vs-requests.db reconciliation (231 real transfers to A
since 2026-09-25, excluding A/B, 229 matched directly, the other 2 traced
to a pre-existing missing payer/amount_usdc field on POST /discover's
'paid' log line rather than any lost delivery) already found zero
evidence of a settled-then-undelivered GET across that whole window.
Kept committed in case a route regresses and this exact test is needed
again - not deleted."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys

import httpx
from eth_account import Account

ADDRESS_A = "0xb3F32bdfe8D07825BC0D7387295aB1D7559BA69d"
ADDRESS_B = "0x3cedc3Cba49c3809EE46B9bf60da75d6607b45Ec"
BASE_URL = "https://x402.agentindex.world"
NETWORK = "eip155:8453"
USDC_CONTRACT = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
BASE_RPC = "https://mainnet.base.org"

TARGET_PATH = "/text/slug"
TARGET_PRICE = 0.002
FUNDING_PATH = "/x402-echo"
FUNDING_PRICE = 0.001


async def usdc_balance(address: str) -> float:
    data = "0x70a08231000000000000000000000000" + address[2:].lower()
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(
            BASE_RPC,
            json={"jsonrpc": "2.0", "id": 1, "method": "eth_call", "params": [{"to": USDC_CONTRACT, "data": data}, "latest"]},
        )
        resp.raise_for_status()
        raw = resp.json().get("result", "0x0")
    return int(raw, 16) / 1_000_000


def _decode_tx_hash(headers: httpx.Headers) -> str | None:
    header = headers.get("payment-response") or headers.get("PAYMENT-RESPONSE")
    if not header:
        return None
    try:
        padded = header + "=" * (-len(header) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(padded))
        for key in ("transaction", "txHash", "tx_hash"):
            if decoded.get(key):
                return decoded[key]
        return json.dumps(decoded)[:200]
    except Exception:
        return header[:60] + "..."


async def pay_get(signer, path: str) -> tuple[int, str | None, str]:
    from x402 import x402Client
    from x402.http.clients.httpx import x402HttpxClient
    from x402.mechanisms.evm.exact.register import register_exact_evm_client

    client = x402Client()
    register_exact_evm_client(client, signer=signer, networks=NETWORK)

    async with x402HttpxClient(client, base_url=BASE_URL, timeout=60.0) as http:
        resp = await http.get(path)
        tx_hash = _decode_tx_hash(resp.headers)
        return resp.status_code, tx_hash, resp.text[:300]


async def main() -> int:
    key_a = (os.getenv("KEY_A") or "").strip()
    key_b = (os.getenv("KEY_B") or "").strip()
    if not key_a or not key_b:
        print("KEY_A/KEY_B manquant(e)", file=sys.stderr)
        return 2
    account_a = Account.from_key(key_a)
    account_b = Account.from_key(key_b)
    del key_a, key_b
    assert account_a.address.lower() == ADDRESS_A.lower()
    assert account_b.address.lower() == ADDRESS_B.lower()

    bal_a = await usdc_balance(ADDRESS_A)
    bal_b = await usdc_balance(ADDRESS_B)
    print(f"A balance (on-chain): {bal_a:.6f} USDC")
    print(f"B balance (on-chain): {bal_b:.6f} USDC")

    shortfall = TARGET_PRICE - bal_b
    if shortfall > 1e-9:
        print(f"Financement B depuis A via {FUNDING_PATH} ({FUNDING_PRICE} USDC)...")
        status, tx, body = await pay_get(account_a, FUNDING_PATH)
        print(f"  {FUNDING_PATH} -> HTTP {status}, tx={tx}")
        if status != 200:
            print(f"ARRET: financement a echoue: {body}", file=sys.stderr)
            return 1
        await asyncio.sleep(8)

    bal_b = await usdc_balance(ADDRESS_B)
    print(f"B balance apres financement: {bal_b:.6f} USDC")

    print(f"\n=== TEST REEL: GET {TARGET_PATH} (route sans handler GET) ===")
    status, tx, body = await pay_get(account_b, TARGET_PATH)
    print(f"HTTP status: {status}")
    print(f"tx (depuis l'en-tete payment-response, si present): {tx}")
    print(f"corps de la reponse: {body!r}")

    await asyncio.sleep(5)
    bal_b_after = await usdc_balance(ADDRESS_B)
    print(f"\nB balance avant: {bal_b:.6f} USDC")
    print(f"B balance apres:  {bal_b_after:.6f} USDC")
    print(f"delta: {bal_b - bal_b_after:.6f} USDC (attendu si paiement regle: {TARGET_PRICE})")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
