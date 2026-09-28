#!/usr/bin/env python3
"""Bazaar bootstrap for POST /v1/chat/completions - PREPARED, NOT RUN.

B pays for one real chat completion against the cheapest PAID model
currently listed by GET /v1/models (by completion price, ties broken by
prompt price; free models - price 0 or a ":free" id suffix, excluded from
the gateway 2026-09-28 - are skipped, see _is_free_model below), with
max_tokens=50, toward A - using the "exact" accepts[] option
(added 2026-09-28 alongside "upto" specifically so a buyer with no ETH and
no Permit2 allowance, like B, isn't excluded). One real settlement is
enough to give the route a live Bazaar listing, same purpose as
bootstrap_bazaar_full.py's ping-pong for the other routes.

Why "exact", not "upto", for this bootstrap
---------------------------------------------
"upto" (Permit2-based) needs the payer to hold ETH (for their own client to
approve Permit2's USDC allowance on-chain) and to have already granted that
allowance - B has neither. "exact" (EIP-3009) needs neither: no prior
approval, no gas paid by the signer, exactly the signing path
bootstrap_bazaar_full.py already uses successfully for every other route.
This script reuses that exact same registration call
(register_exact_evm_client), unlike the abandoned upto-only version of this
script, which used the untested UptoEvmScheme client path.

--dry-run
---------
No real payment. Prints the cheapest model, B's balance, and the ceiling
POST /v1/chat/completions would quote for that model at max_tokens=50 -
and fails loudly (non-zero exit) if that ceiling exceeds 0.002 USDC, since
B's balance is that small and a ceiling above it would mean B literally
cannot sign for the "exact" option (which settles the full ceiling, not a
reduced amount).
"""

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
MAX_TOKENS = 50
MAX_EXPECTED_CEILING_USD = 0.002  # B's balance - see module docstring

DRY_RUN = "--dry-run" in sys.argv


async def usdc_balance(address: str) -> float:
    data = "0x70a08231000000000000000000000000" + address[2:].lower()
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(
            BASE_RPC,
            json={"jsonrpc": "2.0", "id": 1, "method": "eth_call", "params": [{"to": USDC_CONTRACT, "data": data}, "latest"]},
            headers={"User-Agent": "bootstrap-llm-gateway/1.0"},
        )
        resp.raise_for_status()
        raw = resp.json().get("result", "0x0")
    return int(raw, 16) / 1_000_000


def _is_free_model(model_id: str, prompt_price: float, completion_price: float) -> bool:
    """Mirrors app/handlers/llm_gateway.py::_is_free_model exactly (this
    script runs standalone, outside the container, so it can't import the
    app) - free models were excluded from the gateway on 2026-09-28, so the
    bootstrap must pick the cheapest PAID model, not just the cheapest."""
    return prompt_price == 0 or completion_price == 0 or model_id.endswith(":free")


async def cheapest_model_and_ceiling() -> tuple[str, float]:
    """Fetches OpenRouter's real catalog and reimplements
    app/handlers/llm_gateway.py::_price_ceiling_usd's formula directly
    (no import from the deployed app - this script runs standalone,
    outside the container) so the dry-run check reflects what the live
    route will actually quote."""
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get("https://openrouter.ai/api/v1/models")
    data = resp.json()["data"]
    priced = []
    for m in data:
        pricing = m.get("pricing") or {}
        try:
            p, c = float(pricing["prompt"]), float(pricing["completion"])
        except (KeyError, TypeError, ValueError):
            continue
        if p < 0 or c < 0:
            continue
        if _is_free_model(m["id"], p, c):
            continue
        priced.append((c, p, m["id"]))
    priced.sort()
    completion_price, prompt_price, model = priced[0]

    prompt_tokens = 5  # "Say OK." - see the sample request this script sends
    markup = 1.10
    min_settle = 0.001
    ceiling = (prompt_tokens * prompt_price + MAX_TOKENS * completion_price) * markup
    ceiling = max(ceiling, min_settle)
    return model, ceiling


async def pay_chat_completion(signer, model: str) -> tuple[int, str | None, str]:
    """Returns (http_status, tx_hash_or_None, outcome_label). Signs against
    the "exact" accepts[] option - x402Client.register + find_matching_
    requirements pick it automatically since ExactEvmClientScheme is the
    only scheme registered here (upto is deliberately not)."""
    from x402 import x402Client
    from x402.http.clients.httpx import x402HttpxClient
    from x402.mechanisms.evm.exact.register import register_exact_evm_client

    client = x402Client()
    register_exact_evm_client(client, signer=signer, networks=NETWORK)

    body = {
        "model": model,
        "messages": [{"role": "user", "content": "Say OK."}],
        "max_tokens": MAX_TOKENS,
    }

    async with x402HttpxClient(client, base_url=BASE_URL) as http:
        resp = await http.post("/v1/chat/completions", json=body)
        tx_hash = None
        payment_response_b64 = resp.headers.get("payment-response") or resp.headers.get("x-payment-response")
        if payment_response_b64:
            try:
                decoded = json.loads(base64.b64decode(payment_response_b64 + "=" * (-len(payment_response_b64) % 4)))
                tx_hash = decoded.get("transaction")
            except Exception:
                tx_hash = None
        return resp.status_code, tx_hash, resp.text[:300]


async def main() -> int:
    model, ceiling = await cheapest_model_and_ceiling()
    bal_b = await usdc_balance(ADDRESS_B)

    print(f"cheapest model: {model}")
    print(f"computed ceiling (exact, max_tokens={MAX_TOKENS}): ${ceiling:.6f}")
    print(f"B balance (on-chain): {bal_b:.6f} USDC")

    if ceiling > MAX_EXPECTED_CEILING_USD:
        print(
            f"ARRET: ceiling ${ceiling:.6f} exceeds the expected ${MAX_EXPECTED_CEILING_USD} "
            "ceiling this script was written for - B's balance may not cover it. "
            "Re-check before running for real.",
            file=sys.stderr,
        )
        return 1

    if DRY_RUN:
        print("=== DRY RUN - no real payment ===")
        return 0

    key_b = (os.getenv("KEY_B") or "").strip()
    if not key_b:
        print(
            "KEY_B manquant.\n"
            "  read -s KEY_B && export KEY_B && \\\n"
            "    .venv/bin/python scripts/bootstrap_llm_gateway.py && \\\n"
            "    unset KEY_B",
            file=sys.stderr,
        )
        return 2

    account_b = Account.from_key(key_b)
    del key_b  # never referenced again; not logged, not printed
    if account_b.address.lower() != ADDRESS_B.lower():
        print(f"KEY_B ne correspond pas a B ({account_b.address} != {ADDRESS_B}) - arret.", file=sys.stderr)
        return 1
    print(f"B confirme: {account_b.address}")

    if bal_b < ceiling:
        print(f"ARRET: solde B ({bal_b:.6f}) < ceiling ({ceiling:.6f}).", file=sys.stderr)
        return 1

    status, tx_hash, outcome = await pay_chat_completion(account_b, model)
    print(f"POST /v1/chat/completions -> HTTP {status}, tx={tx_hash}, {outcome}")
    return 0 if status == 200 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
