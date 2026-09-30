#!/usr/bin/env python3
"""Bazaar bootstrap for all 12 routes added 2026-09-30 (PACK 1 + PACK 2) -
PREPARED, NOT RUN.

B pays once for each of the 12 new routes toward A, using the route's own
real catalog sample as the request body (fetched live from
/.well-known/x402 - the same sample the Bazaar listing and GET /*/sample
both already serve, per the single-source-of-truth fix in this same
batch). One real settlement per route is enough to give each one a live
Bazaar listing, same purpose as bootstrap_research.py /
bootstrap_token_risk.py for earlier routes - this script generalizes that
same pattern to run once for a whole batch instead of one route at a time.

PACK 1 (fixed price, config.PRICE_*): sentiment, classify, intent,
spam-check, toxicity, language, pii-check.

PACK 2 (dynamic price, ceiling from max_tokens): llm/claude-sonnet,
llm/gpt-mini, llm/gemini-flash, llm/llama, llm/deepseek. Price is NOT
hardcoded here - fetched live via an unauthenticated request per route
(the same 402 challenge a real buyer would see), since a hardcoded number
would drift the moment OpenRouter's per-token rates change. This also
means fetch_route() below matches on the FULL path after BASE_URL (not
just the last URL segment, as the single-route bootstrap scripts before
it did) - needed to disambiguate "llm/claude-sonnet" from "claude-sonnet"
alone, which does not exist as a route.

Funding step (same pattern as bootstrap_research.py, 2026-09-28)
------------------------------------------------------------------
If B's local balance doesn't cover the SUM of all 12 real prices, A pays
/x402-echo (fixed payTo=B, $0.001 each) exactly enough times to close the
shortfall, with an 8s pause after each real settlement
(POST_SETTLE_PAUSE_S) before B's first real payment. Needs both KEY_A and
KEY_B. A further POST_SETTLE_PAUSE_S pause is added between each of the 12
target payments too - this script settles many payments in one run,
unlike the single-target bootstrap scripts it generalizes, so the same
chain-catch-up reasoning applies throughout, not just after funding.

--dry-run
---------
No real payment. Prints A and B's balances, each route's live-fetched
price, the total, how many fundings (if any) the total would require, and
fails loudly (non-zero exit) if A's balance can't cover the needed
funding.
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
    "sentiment", "classify", "intent", "spam-check", "toxicity", "language", "pii-check",
    "llm/claude-sonnet", "llm/gpt-mini", "llm/gemini-flash", "llm/llama", "llm/deepseek",
]

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
            headers={"User-Agent": "bootstrap-new-routes/1.0"},
        )
        resp.raise_for_status()
        raw = resp.json().get("result", "0x0")
    return int(raw, 16) / 1_000_000


# --- route + price discovery, both live from the real catalog ---------------

async def fetch_route(path: str) -> dict:
    """path: e.g. "sentiment" or "llm/claude-sonnet" (no leading slash).
    Matches on the FULL path after BASE_URL, not just the last URL
    segment - needed for Pack 2's nested /llm/* paths."""
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
    """Live-fetched from the route's own 402 challenge, using its real
    catalog sample body - the same quote a real buyer would see. Never
    hardcoded, since Pack 2's routes are dynamically priced from
    max_tokens x the model's real OpenRouter rate."""
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


# --- payment plumbing, ported from bootstrap_research.py ---------------------

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
    # Always returns the FULL response body as the 3rd element, success or
    # not - this is the real end-to-end test of what each route actually
    # delivers for real money, not just a status code.
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
    return n  # unlike the single-route scripts, the total here is a sum of
    # 12 independently-quoted prices, not guaranteed to land on an exact
    # multiple of FUNDING_PRICE - round up (see the +1-if-short check in
    # fund_b_if_needed) rather than asserting an exact multiple.


async def fund_b_if_needed(account_a, bal_a: float, bal_b: float, total_price: float) -> tuple[float, float]:
    shortfall = total_price - bal_b
    n_fundings = _fundings_needed_for_shortfall(shortfall)
    if n_fundings and n_fundings * FUNDING_PRICE < shortfall:
        n_fundings += 1  # rounding landed short - fund one more

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

    print("Fetching live route + price for each of the 12 targets...")
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
            "    .venv/bin/python scripts/bootstrap_new_routes.py && \\\n"
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
