#!/usr/bin/env python3
"""Refresh every already-indexed CDP Bazaar route with a real settlement.

State this script assumes (2026-09-26)
---------------------------------------------------------------------------
payTo is now fixed per route in app code (app/x402_setup.py), not toggled
via X402_PAY_TO anymore: /x402-echo always pays to B
(config.X402_ECHO_PAY_TO), every other route always pays to A
(config.X402_PAY_TO). This script therefore never touches .env, never
rebuilds or recreates the container - it only ever sends payments, against
whatever payTo the live 402 challenge already advertises.

Flow
----
1. A pays /x402-echo once -> funds B (fixed payTo=B on this one route).
2. B pays each of the already-indexed routes once -> refreshes each
   listing toward A (fixed payTo=A, unchanged) so CDP Bazaar re-crawls a
   recent settlement per resource.

Already-indexed routes (INDEXED_SLUGS): search, news, can-pay, probe,
wallet-balance, gas-price, wallet-intelligence, agent-health, pdf,
web-read. Anything else (discover, weather, crypto, translate, jobs,
extract, summarize, fact-check, tip, agent-claim) has never been settled
and is intentionally left out of the refresh - some have real side effects
(jobs dispatches a research job, tip and agent-claim mutate state) or a
cost unfit for a routine refresh ping.

Per-payment outcome
--------------------
- 200 -> settled, tx noted.
- invalidReason "amount_too_low" -> noted (not expected at these prices,
  handled defensively).
- "execution reverted" (exception or response body) -> treated as
  transient (RPC balance-read lag right after a settlement lands) - short
  sleep, retry the same payment, up to MAX_RETRIES.
- anything else -> the whole run stops immediately.

Usage
-----
  read -s KEY_A && export KEY_A && \\
    read -s KEY_B && export KEY_B && \\
    .venv/bin/python scripts/bootstrap_bazaar_settle.py && \\
    unset KEY_A KEY_B

Safety
------
- Refuses to run unless KEY_A derives to ADDRESS_A and KEY_B to ADDRESS_B.
- Never prints, logs or otherwise surfaces KEY_A or KEY_B.
- No SSH, no .env edit, no docker rebuild/recreate - payments only.
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
FUNDING_SLUG = "x402-echo"
INDEXED_SLUGS = [
    "search", "news", "can-pay", "probe", "wallet-balance",
    "gas-price", "wallet-intelligence", "agent-health", "pdf", "web-read",
]
BASE_URL = "https://x402.agentindex.world"
NETWORK = "eip155:8453"
AMOUNT_TOO_LOW_MARKER = "amount_too_low"
EXECUTION_REVERTED_MARKER = "execution reverted"
MAX_RETRIES = 5
RETRY_DELAY_S = 10

USDC_CONTRACT = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
BASE_RPC = "https://mainnet.base.org"


# --- Base mainnet USDC balance ---------------------------------------------

async def usdc_balance(address: str) -> float:
    data = "0x70a08231000000000000000000000000" + address[2:].lower()
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(
            BASE_RPC,
            json={"jsonrpc": "2.0", "id": 1, "method": "eth_call", "params": [{"to": USDC_CONTRACT, "data": data}, "latest"]},
            headers={"User-Agent": "bootstrap-bazaar/1.0"},
        )
        resp.raise_for_status()
        raw = resp.json().get("result", "0x0")
    return int(raw, 16) / 1_000_000


# --- route discovery (live, never hand-copied) ------------------------------

def _price(accepts: list[dict]) -> float | None:
    if not accepts:
        return None
    price = accepts[0].get("price")
    if not isinstance(price, str):
        return None
    try:
        return float(price.lstrip("$"))
    except ValueError:
        return None


async def fetch_routes(slugs: list[str]) -> dict[str, dict]:
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(f"{BASE_URL}/.well-known/x402")
        resp.raise_for_status()
        resources = resp.json()["resources"]

    by_slug: dict[str, dict] = {}
    for r in resources:
        slug = r["resource"].rstrip("/").rsplit("/", 1)[-1]
        if slug not in slugs:
            continue
        current = by_slug.get(slug)
        if current is not None and current["method"] == "GET":
            continue
        by_slug[slug] = {
            "slug": slug,
            "url": r["resource"],
            "method": r["method"].upper(),
            "price": _price(r.get("accepts") or []),
            "bazaar_input": (r.get("extensions") or {}).get("bazaar", {}).get("info", {}).get("input", {}),
        }
    missing = [s for s in slugs if s not in by_slug]
    if missing:
        raise RuntimeError(f"route(s) introuvable(s) dans /.well-known/x402: {missing}")
    return by_slug


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
    if AMOUNT_TOO_LOW_MARKER in body_text:
        return "amount_too_low"
    if EXECUTION_REVERTED_MARKER in body_text.lower():
        return "execution_reverted"
    return "autre_erreur"


def _print_table(results: list[tuple[str, str, str | None, str, str | None]]) -> None:
    print("\n=== resume ===")
    print(f"{'route':26s} {'payeur':44s} {'resultat'}")
    for slug, payer, tx, outcome, detail in results:
        label = {
            "reglee": f"reglee, tx={tx}",
            "amount_too_low": "amount_too_low",
            "execution_reverted": "execution reverted - non reglee, abandonnee",
            "autre_erreur": f"autre erreur: {detail}",
        }[outcome]
        print(f"{slug:26s} {payer:44s} {label}")


async def _pay_with_retry(signer: Account, route: dict) -> tuple[str, str | None, str | None]:
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
            if EXECUTION_REVERTED_MARKER in str(exc).lower() and attempt < MAX_RETRIES:
                print(f"  {route['slug']}: execution reverted (essai {attempt}/{MAX_RETRIES}), nouvel essai dans {RETRY_DELAY_S}s")
                await asyncio.sleep(RETRY_DELAY_S)
                continue
            return "autre_erreur", None, str(exc)[:300]

        outcome = _outcome(status, body_text)
        if outcome == "execution_reverted" and attempt < MAX_RETRIES:
            print(f"  {route['slug']}: execution reverted (essai {attempt}/{MAX_RETRIES}), nouvel essai dans {RETRY_DELAY_S}s")
            await asyncio.sleep(RETRY_DELAY_S)
            continue
        detail = None if outcome == "reglee" else body_text[:300]
        return outcome, tx, detail

    return "execution_reverted", None, None


async def run(account_a: Account, account_b: Account, results: list) -> None:
    funding_route = (await fetch_routes([FUNDING_SLUG]))[FUNDING_SLUG]

    print(f"\n--- financement: A paie {FUNDING_SLUG} (${funding_route['price']}) -> B ---")
    outcome, tx, detail = await _pay_with_retry(account_a, funding_route)
    results.append((FUNDING_SLUG, ADDRESS_A, tx, outcome, detail))
    if outcome != "reglee":
        raise RuntimeError(f"echec du financement de B via {FUNDING_SLUG}: {detail}")
    print(f"  financement reglee, tx={tx}")

    routes = await fetch_routes(INDEXED_SLUGS)
    bal_b = await usdc_balance(ADDRESS_B)
    total_price = sum(r["price"] for r in routes.values())
    print(f"\nsolde B apres financement: {bal_b:.6f} USDC, cout total du rafraichissement: {total_price:.6f} USDC")
    if bal_b < total_price:
        raise RuntimeError(f"solde B insuffisant pour rafraichir les {len(routes)} routes: {bal_b:.6f} < {total_price:.6f}")

    for slug in INDEXED_SLUGS:
        route = routes[slug]
        print(f"\n--- {slug} (${route['price']}): B paie -> A ---")
        outcome, tx, detail = await _pay_with_retry(account_b, route)
        results.append((slug, ADDRESS_B, tx, outcome, detail))
        if outcome == "reglee":
            print(f"  {slug:22s} status=200 tx={tx}")
        elif outcome == "amount_too_low":
            print(f"  {slug:22s} amount_too_low - notee")
        else:
            raise RuntimeError(f"erreur sur {slug}: {detail}")


async def main() -> int:
    key_a = (os.getenv("KEY_A") or "").strip()
    key_b = (os.getenv("KEY_B") or "").strip()
    if not key_a or not key_b:
        print(
            "KEY_A et/ou KEY_B manquant(e).\n"
            "  read -s KEY_A && export KEY_A && \\\n"
            "    read -s KEY_B && export KEY_B && \\\n"
            "    .venv/bin/python scripts/bootstrap_bazaar_settle.py && \\\n"
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

    results: list[tuple[str, str, str | None, str, str | None]] = []
    exit_code = 0
    try:
        await run(account_a, account_b, results)
    except Exception as exc:
        print(f"\nARRET: {exc}", file=sys.stderr)
        exit_code = 1

    _print_table(results)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
