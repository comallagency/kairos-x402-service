#!/usr/bin/env python3
"""Bazaar bootstrap for POST /research - PREPARED, NOT RUN.

B pays once for a real /research call (a fixed, stable query - "What is
the current price of Bitcoin?", the "price" category, the fastest and most
reliable path measured for this route - see app/handlers/research.py's
module docstring for the real latency data behind that choice) toward A.
One real settlement is enough to give the route a live Bazaar listing,
same purpose as bootstrap_bazaar_full.py's ping-pong for the other routes,
bootstrap_llm_gateway.py for the LLM gateway, and bootstrap_token_risk.py
for /token-risk.

/research has a single flat price ($0.005, config.PRICE_RESEARCH) and only
the "exact" accepts[] option - no dynamic ceiling, no dual-scheme choice.
Same simple shape as bootstrap_token_risk.py.

Funding step (same pattern as the other bootstrap scripts, 2026-09-28)
---------------------------------------------------------------------
If B's local balance doesn't cover the $0.005 price, A pays /x402-echo
(fixed payTo=B, $0.001 each) exactly enough times to close the shortfall,
with an 8s pause after each real settlement (POST_SETTLE_PAUSE_S) before
B's real payment. Needs both KEY_A and KEY_B.

--dry-run
---------
No real payment. Prints A and B's balances, how many fundings (if any) the
$0.005 price would require, and fails loudly (non-zero exit) if A's balance
can't cover the needed funding.
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

TARGET_PRICE = 0.005  # config.PRICE_RESEARCH
TARGET_QUERY = "What is the current price of Bitcoin?"  # "price" category - fastest, most reliable path measured

FUNDING_SLUG = "x402-echo"
FUNDING_PRICE = 0.001  # config.PRICE_X402_ECHO
MAX_RETRIES = 3
RETRY_DELAY_S = 10
POST_SETTLE_PAUSE_S = 8
EPSILON = 1e-9

DRY_RUN = "--dry-run" in sys.argv


async def usdc_balance(address: str) -> float:
    data = "0x70a08231000000000000000000000000" + address[2:].lower()
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(
            BASE_RPC,
            json={"jsonrpc": "2.0", "id": 1, "method": "eth_call", "params": [{"to": USDC_CONTRACT, "data": data}, "latest"]},
            headers={"User-Agent": "bootstrap-research/1.0"},
        )
        resp.raise_for_status()
        raw = resp.json().get("result", "0x0")
    return int(raw, 16) / 1_000_000


# --- funding step, ported from bootstrap_token_risk.py / bootstrap_bazaar_full.py -----

async def fetch_route(slug: str) -> dict:
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(f"{BASE_URL}/.well-known/x402")
        resp.raise_for_status()
        resources = resp.json()["resources"]

    for r in resources:
        if r["resource"].rstrip("/").rsplit("/", 1)[-1] != slug:
            continue
        return {
            "slug": slug,
            "url": r["resource"],
            "method": r["method"].upper(),
            "bazaar_input": (r.get("extensions") or {}).get("bazaar", {}).get("info", {}).get("input", {}),
        }
    raise RuntimeError(f"route introuvable dans /.well-known/x402: {slug}")


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
        return json.dumps(decoded)[:120]
    except Exception:
        return header[:60] + "..."


async def pay_once(http, route: dict) -> tuple[int, str | None, str]:
    bazaar_input = route["bazaar_input"]
    if route["method"] == "GET":
        params = bazaar_input.get("queryParams") or {}
        resp = await http.get(route["url"], params=params)
    else:
        body = bazaar_input.get("body") if bazaar_input.get("bodyType") == "json" else None
        resp = await http.post(route["url"], json=body or {})
    tx_hash = _decode_tx_hash(resp.headers)
    return resp.status_code, tx_hash, resp.text


def _outcome(status: int, body_text: str) -> str:
    if status == 200:
        return "reglee"
    if "amount_too_low" in body_text:
        return "amount_too_low"
    return "a_retenter"


async def _pay_with_retry(signer, route: dict) -> tuple[str, str | None, str | None]:
    if DRY_RUN:
        return "reglee", "SIMULATED", None

    from x402 import x402Client
    from x402.http.clients.httpx import x402HttpxClient
    from x402.mechanisms.evm.exact.register import register_exact_evm_client

    client = x402Client()
    register_exact_evm_client(client, signer=signer, networks=NETWORK)

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            async with x402HttpxClient(client, timeout=60.0) as http:
                status, tx, body_text = await pay_once(http, route)
        except Exception as exc:
            if attempt < MAX_RETRIES:
                print(f"  {route['slug']}: exception (essai {attempt}/{MAX_RETRIES}: {str(exc)[:150]}), nouvel essai dans {RETRY_DELAY_S}s")
                await asyncio.sleep(RETRY_DELAY_S)
                continue
            return "a_retenter", None, str(exc)[:300]

        outcome = _outcome(status, body_text)
        if outcome == "amount_too_low":
            return outcome, tx, body_text[:300]
        if outcome == "a_retenter" and attempt < MAX_RETRIES:
            print(f"  {route['slug']}: status={status} non-200 (essai {attempt}/{MAX_RETRIES}, corps: {body_text[:100]!r}), nouvel essai dans {RETRY_DELAY_S}s")
            await asyncio.sleep(RETRY_DELAY_S)
            continue
        detail = None if outcome == "reglee" else body_text[:300]
        return outcome, tx, detail

    return "a_retenter", None, "essais epuises"


def _fundings_needed_for_shortfall(shortfall: float) -> int:
    if shortfall <= EPSILON:
        return 0
    n = round(shortfall / FUNDING_PRICE)
    if n < 1:
        n = 1
    if abs(n * FUNDING_PRICE - shortfall) > 1e-6:
        raise RuntimeError(f"écart {shortfall} n'est pas un multiple exact de {FUNDING_PRICE} - financement impossible")
    return n


async def fund_b_if_needed(account_a, bal_a: float, bal_b: float, price: float) -> tuple[float, float]:
    shortfall = price - bal_b
    n_fundings = _fundings_needed_for_shortfall(shortfall)

    if n_fundings == 0:
        print(f"B a deja {bal_b:.6f} >= prix {price:.6f}, aucun financement necessaire")
        return bal_a, bal_b

    print(f"B a {bal_b:.6f}, il manque {shortfall:.6f} -> {n_fundings} financement(s) de {FUNDING_PRICE} via /x402-echo")
    funding_route = await fetch_route(FUNDING_SLUG)

    for i in range(1, n_fundings + 1):
        if bal_a + EPSILON < FUNDING_PRICE:
            raise RuntimeError(f"solde local A insuffisant ({bal_a:.6f}) pour le financement {i}/{n_fundings}")
        outcome, tx, detail = await _pay_with_retry(account_a, funding_route)
        if outcome != "reglee":
            raise RuntimeError(f"echec financement ({i}/{n_fundings}): {detail}")
        bal_a -= FUNDING_PRICE
        bal_b += FUNDING_PRICE
        print(f"  financement {i}/{n_fundings} regle, tx={tx} | solde local A={bal_a:.6f} B={bal_b:.6f}")
        if not DRY_RUN:
            print(f"  pause {POST_SETTLE_PAUSE_S}s (laisser la chaine rattraper)")
            await asyncio.sleep(POST_SETTLE_PAUSE_S)

    return bal_a, bal_b


# --- target payment ----------------------------------------------------------

async def pay_research(signer) -> tuple[int, str | None, str]:
    from x402 import x402Client
    from x402.http.clients.httpx import x402HttpxClient
    from x402.mechanisms.evm.exact.register import register_exact_evm_client

    client = x402Client()
    register_exact_evm_client(client, signer=signer, networks=NETWORK)

    async with x402HttpxClient(client, base_url=BASE_URL, timeout=15.0) as http:
        resp = await http.post("/research", json={"query": TARGET_QUERY})
        tx_hash = _decode_tx_hash(resp.headers)
        return resp.status_code, tx_hash, resp.text[:400]


async def main() -> int:
    bal_a = await usdc_balance(ADDRESS_A)
    bal_b = await usdc_balance(ADDRESS_B)

    print(f"target price (POST /research): ${TARGET_PRICE:.6f}")
    print(f"target query: {TARGET_QUERY!r}")
    print(f"A balance (on-chain): {bal_a:.6f} USDC")
    print(f"B balance (on-chain): {bal_b:.6f} USDC")

    if DRY_RUN:
        shortfall = TARGET_PRICE - bal_b
        n_fundings = _fundings_needed_for_shortfall(shortfall)
        print(f"fundings needed: {n_fundings} (shortfall {max(shortfall, 0):.6f})")
        if n_fundings and bal_a + EPSILON < n_fundings * FUNDING_PRICE:
            print(
                f"ARRET: solde A ({bal_a:.6f}) insuffisant pour {n_fundings} financement(s) "
                f"({n_fundings * FUNDING_PRICE:.6f}).",
                file=sys.stderr,
            )
            return 1
        print("=== DRY RUN - no real payment ===")
        return 0

    key_a = (os.getenv("KEY_A") or "").strip()
    key_b = (os.getenv("KEY_B") or "").strip()
    if not key_a or not key_b:
        print(
            "KEY_A et/ou KEY_B manquant(e).\n"
            "  read -s KEY_A && export KEY_A && \\\n"
            "    read -s KEY_B && export KEY_B && \\\n"
            "    .venv/bin/python scripts/bootstrap_research.py && \\\n"
            "    unset KEY_A KEY_B",
            file=sys.stderr,
        )
        return 2

    account_a = Account.from_key(key_a)
    account_b = Account.from_key(key_b)
    del key_a, key_b  # never referenced again; not logged, not printed
    if account_a.address.lower() != ADDRESS_A.lower():
        print(f"KEY_A ne correspond pas a A ({account_a.address} != {ADDRESS_A}) - arret.", file=sys.stderr)
        return 1
    if account_b.address.lower() != ADDRESS_B.lower():
        print(f"KEY_B ne correspond pas a B ({account_b.address} != {ADDRESS_B}) - arret.", file=sys.stderr)
        return 1
    print(f"A confirme: {account_a.address}")
    print(f"B confirme: {account_b.address}")

    try:
        bal_a, bal_b = await fund_b_if_needed(account_a, bal_a, bal_b, TARGET_PRICE)
    except Exception as exc:
        print(f"ARRET: {exc}", file=sys.stderr)
        return 1

    if bal_b + EPSILON < TARGET_PRICE:
        print(f"ARRET: solde B ({bal_b:.6f}) < prix ({TARGET_PRICE:.6f}) apres financement.", file=sys.stderr)
        return 1

    status, tx_hash, body = await pay_research(account_b)
    print(f"POST /research -> HTTP {status}, tx={tx_hash}, {body[:200]}")
    return 0 if status == 200 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
