#!/usr/bin/env python3
"""Bazaar bootstrap for POST /v1/chat/completions - PREPARED, NOT RUN.

B pays for one real chat completion against the cheapest model currently
listed by GET /v1/models (by completion price, ties broken by prompt
price), with max_tokens=50, toward A. One real "upto" settlement is enough
to give the route a live Bazaar listing, same purpose as
bootstrap_bazaar_full.py's ping-pong for the "exact"-scheme routes.

UNVERIFIED - read before running
---------------------------------
This is the first script in this codebase to sign an "upto" (Permit2-based)
payment. bootstrap_bazaar_full.py's signing path
(x402.mechanisms.evm.exact.register.register_exact_evm_client) only knows
the "exact" scheme; this uses the analogous upto client scheme
(x402.mechanisms.evm.upto.client.UptoEvmScheme) by the same registration
pattern, but that pattern itself has never been exercised end-to-end
against the live CDP facilitator - only the SERVER side (the 402 challenge
itself) has been verified live (2026-09-28: a real `curl` against
/v1/chat/completions returned a well-formed scheme="upto" challenge with a
facilitatorAddress, confirming CDP's facilitator advertises upto support -
it does not confirm settlement succeeds).

The other open question is Permit2's own prerequisite: unlike "exact"
(EIP-3009, needs no prior on-chain approval), "upto" payments settle via
Permit2's permitWitnessTransferFrom, which requires wallet B to have
already granted Permit2 an ERC-20 allowance on USDC. check_permit2_allowance()
below only READS that allowance and warns if it looks insufficient - it
does not submit an approve() transaction. The facilitator's own settlement
code (x402/mechanisms/evm/upto/permit2_utils.py) has fallback paths
mentioning EIP-2612 permit and "erc20 approval", which may mean the
facilitator can obtain allowance itself as part of settling - or may not.
This was not resolved (it would require an actual test payment to observe),
so treat the allowance check's warning as "investigate before relying on
this", not as a green light either way once it passes.

Recommended before treating this as routine: run it once, read the result
table, and check the tx on Basescan before assuming it will keep working
unattended.
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
PERMIT2_ADDRESS = "0x000000000022D473030F116dDEE9F6B43aC78BA"  # canonical Permit2, all EVM chains
BASE_RPC = "https://mainnet.base.org"
MAX_TOKENS = 50

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


async def check_permit2_allowance(owner: str) -> float:
    """Read-only ERC-20 allowance(owner, Permit2) on USDC - see module
    docstring: this warns, it does not fix a low allowance."""
    selector = "0xdd62ed3e"
    owner_padded = owner[2:].lower().rjust(64, "0")
    spender_padded = PERMIT2_ADDRESS[2:].lower().rjust(64, "0")
    data = selector + owner_padded + spender_padded
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(
            BASE_RPC,
            json={"jsonrpc": "2.0", "id": 1, "method": "eth_call", "params": [{"to": USDC_CONTRACT, "data": data}, "latest"]},
            headers={"User-Agent": "bootstrap-llm-gateway/1.0"},
        )
        resp.raise_for_status()
        raw = resp.json().get("result", "0x0")
    return int(raw, 16) / 1_000_000


async def cheapest_model() -> str:
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
        priced.append((c, p, m["id"]))
    priced.sort()
    return priced[0][2]


async def pay_chat_completion(signer, model: str) -> tuple[int, str | None, str]:
    """Returns (http_status, tx_hash_or_None, outcome_label). Mirrors
    bootstrap_bazaar_full.py's pay_once() shape, but for the upto scheme."""
    from x402 import x402Client
    from x402.http.clients.httpx import x402HttpxClient
    from x402.mechanisms.evm.upto.client import UptoEvmScheme as UptoEvmClientScheme

    client = x402Client()
    client.register(NETWORK, UptoEvmClientScheme(signer))

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
    if DRY_RUN:
        model = await cheapest_model()
        bal_b = await usdc_balance(ADDRESS_B)
        allowance_b = await check_permit2_allowance(ADDRESS_B)
        print(f"=== DRY RUN - no real payment ===\ncheapest model: {model}\nB balance: {bal_b:.6f} USDC\nB Permit2 allowance on USDC: {allowance_b:.6f}")
        if allowance_b <= 0:
            print("WARNING: B has no Permit2 allowance on USDC - see module docstring before running for real.")
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

    model = await cheapest_model()
    bal_b = await usdc_balance(ADDRESS_B)
    allowance_b = await check_permit2_allowance(ADDRESS_B)
    print(f"B confirme: {account_b.address}")
    print(f"cheapest model: {model}")
    print(f"solde B (lecture on-chain): {bal_b:.6f} USDC")
    print(f"allowance Permit2 de B sur USDC: {allowance_b:.6f}")
    if allowance_b <= 0:
        print(
            "ARRET: B n'a pas d'allowance Permit2 sur USDC - un paiement upto "
            "echouera a la liquidation. Voir le docstring du module avant "
            "de continuer (approbation ERC-20 manuelle probablement requise "
            "en amont, jamais automatisee ici).",
            file=sys.stderr,
        )
        return 1

    status, tx_hash, outcome = await pay_chat_completion(account_b, model)
    print(f"POST /v1/chat/completions -> HTTP {status}, tx={tx_hash}, {outcome}")
    return 0 if status == 200 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
