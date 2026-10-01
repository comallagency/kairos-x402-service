#!/usr/bin/env python3
"""Bazaar bootstrap for all 51 pure-compute routes added 2026-10-02
(geo/time/validate/number/fraction/money/text/encoding/hash/json/regex/
stats) - PREPARED, NOT RUN.

Same shape as scripts/bootstrap_new_routes.py, generalized from 12 targets
to 51: B pays once for each new route toward A, using the route's own real
catalog sample as the request body (fetched live from /.well-known/x402),
to give each one a live Bazaar listing.

All 51 share the same fixed price ($0.002, app.purecalc.registry), unlike
PACK 2's dynamic per-token pricing in bootstrap_new_routes.py - price is
still fetched live from each route's own 402 challenge rather than
hardcoded, same reasoning (the quote a real buyer would see, never
assumed), and it also means a route whose price changes before this ever
runs is handled correctly with zero edit needed here.

Funding step, retry logic, --dry-run: identical to
scripts/bootstrap_new_routes.py - see that script's docstring for the
full explanation. Needs both KEY_A and KEY_B (never logged, never printed).
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

TARGET_PATHS = [
    # Geospatial (7)
    "geo/distance", "geo/bearing", "geo/midpoint", "geo/geohash",
    "geo/point-in-polygon", "geo/bbox", "geo/dms",
    # Dates (8)
    "time/convert", "time/add", "time/between", "time/business-days",
    "time/iso-week", "time/parse", "time/format", "time/humanize",
    # Validations (9)
    "validate/iban", "validate/vin", "validate/isbn", "validate/luhn",
    "validate/ean", "validate/imei", "validate/routing", "validate/isin",
    "validate/siret",
    # Units and numbers (8)
    "unit/convert", "number/roman", "number/words", "number/radix",
    "number/ordinal", "fraction/simplify", "money/format", "money/allocate",
    # Text and encodings (8)
    "text/case", "text/diff", "text/slug", "encoding/transcode",
    "encoding/detect", "hash/hash", "hash/hmac", "text/readability",
    # Data (7)
    "json/query", "json/diff", "json/flatten", "json/schema-infer",
    "json/validate", "regex/test", "regex/replace",
    # Statistics (4)
    "stats/summary", "stats/correlation", "stats/regression", "stats/percentile",
]
assert len(TARGET_PATHS) == 51

FUNDING_PATH = "x402-echo"
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
            headers={"User-Agent": "bootstrap-purecalc-routes/1.0"},
        )
        resp.raise_for_status()
        raw = resp.json().get("result", "0x0")
    return int(raw, 16) / 1_000_000


# --- route + price discovery, both live from the real catalog ---------------

async def fetch_route(path: str) -> dict:
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(f"{BASE_URL}/.well-known/x402")
        resp.raise_for_status()
        resources = resp.json()["resources"]

    target_url = f"{BASE_URL}/{path}"
    for r in resources:
        if r["resource"].rstrip("/") != target_url:
            continue
        return {
            "path": path,
            "url": r["resource"],
            "method": r["method"].upper(),
            "bazaar_input": (r.get("extensions") or {}).get("bazaar", {}).get("info", {}).get("input", {}),
        }
    raise RuntimeError(f"route introuvable dans /.well-known/x402: {path}")


async def fetch_price(route: dict) -> float:
    bazaar_input = route["bazaar_input"]
    async with httpx.AsyncClient(timeout=20.0) as client:
        if route["method"] == "GET":
            resp = await client.get(route["url"], params=(bazaar_input.get("queryParams") or {}))
        else:
            body = bazaar_input.get("body") if bazaar_input.get("bodyType") == "json" else None
            resp = await client.post(route["url"], json=body or {})
    data = resp.json()
    amount_atomic = int(data["accepts"][0]["amount"])
    return amount_atomic / 1_000_000


# --- payment plumbing, ported from bootstrap_new_routes.py -------------------

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
                print(f"  {route['path']}: exception (essai {attempt}/{MAX_RETRIES}: {str(exc)[:150]}), nouvel essai dans {RETRY_DELAY_S}s")
                await asyncio.sleep(RETRY_DELAY_S)
                continue
            return "a_retenter", None, str(exc)[:300]

        outcome = _outcome(status, body_text)
        if outcome == "amount_too_low":
            return outcome, tx, body_text
        if outcome == "a_retenter" and attempt < MAX_RETRIES:
            print(f"  {route['path']}: status={status} non-200 (essai {attempt}/{MAX_RETRIES}, corps: {body_text[:100]!r}), nouvel essai dans {RETRY_DELAY_S}s")
            await asyncio.sleep(RETRY_DELAY_S)
            continue
        return outcome, tx, body_text

    return "a_retenter", None, "essais epuises"


def _fundings_needed_for_shortfall(shortfall: float) -> int:
    if shortfall <= EPSILON:
        return 0
    n = round(shortfall / FUNDING_PRICE)
    if n < 1:
        n = 1
    return n


async def fund_b_if_needed(account_a, bal_a: float, bal_b: float, total_price: float) -> tuple[float, float]:
    shortfall = total_price - bal_b
    n_fundings = _fundings_needed_for_shortfall(shortfall)
    if n_fundings and n_fundings * FUNDING_PRICE < shortfall:
        n_fundings += 1

    if n_fundings == 0:
        print(f"B a deja {bal_b:.6f} >= total requis {total_price:.6f}, aucun financement necessaire")
        return bal_a, bal_b

    print(f"B a {bal_b:.6f}, il manque {shortfall:.6f} -> {n_fundings} financement(s) de {FUNDING_PRICE} via /x402-echo")
    funding_route = await fetch_route(FUNDING_PATH)

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


async def main() -> int:
    bal_a = await usdc_balance(ADDRESS_A)
    bal_b = await usdc_balance(ADDRESS_B)
    print(f"A balance (on-chain): {bal_a:.6f} USDC")
    print(f"B balance (on-chain): {bal_b:.6f} USDC")

    print(f"Fetching live route + price for each of the {len(TARGET_PATHS)} targets...")
    routes = []
    total_price = 0.0
    for path in TARGET_PATHS:
        route = await fetch_route(path)
        price = await fetch_price(route)
        routes.append((route, price))
        total_price += price
        print(f"  {path}: ${price:.6f}")
    print(f"total required: ${total_price:.6f}")

    if DRY_RUN:
        shortfall = total_price - bal_b
        n_fundings = _fundings_needed_for_shortfall(shortfall)
        if n_fundings and n_fundings * FUNDING_PRICE < shortfall:
            n_fundings += 1
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
            "    .venv/bin/python scripts/bootstrap_purecalc_routes.py && \\\n"
            "    unset KEY_A KEY_B",
            file=sys.stderr,
        )
        return 2

    account_a = Account.from_key(key_a)
    account_b = Account.from_key(key_b)
    del key_a, key_b
    if account_a.address.lower() != ADDRESS_A.lower():
        print(f"KEY_A ne correspond pas a A ({account_a.address} != {ADDRESS_A}) - arret.", file=sys.stderr)
        return 1
    if account_b.address.lower() != ADDRESS_B.lower():
        print(f"KEY_B ne correspond pas a B ({account_b.address} != {ADDRESS_B}) - arret.", file=sys.stderr)
        return 1
    print(f"A confirme: {account_a.address}")
    print(f"B confirme: {account_b.address}")

    try:
        bal_a, bal_b = await fund_b_if_needed(account_a, bal_a, bal_b, total_price)
    except Exception as exc:
        print(f"ARRET: {exc}", file=sys.stderr)
        return 1

    if bal_b + EPSILON < total_price:
        print(f"ARRET: solde B ({bal_b:.6f}) < total requis ({total_price:.6f}) apres financement.", file=sys.stderr)
        return 1

    failures = []
    for i, (route, price) in enumerate(routes, start=1):
        outcome, tx, body_text = await _pay_with_retry(account_b, route)
        status_word = "OK" if outcome == "reglee" else f"ECHEC({outcome})"
        print(f"[{i}/{len(routes)}] POST /{route['path']} -> {status_word} tx={tx} prix=${price:.6f}")
        print(f"  reponse complete: {body_text}")
        if outcome != "reglee":
            failures.append(route["path"])
        if not DRY_RUN and i < len(routes):
            await asyncio.sleep(POST_SETTLE_PAUSE_S)

    if failures:
        print(f"ARRET PARTIEL: {len(failures)}/{len(routes)} route(s) en echec: {failures}", file=sys.stderr)
        return 1
    print(f"OK: {len(routes)}/{len(routes)} routes reglees.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
