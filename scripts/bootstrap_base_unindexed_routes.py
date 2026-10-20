#!/usr/bin/env python3
"""Bazaar FIRST-SETTLEMENT bootstrap for /due-diligence, /company-leads -
PREPARED, NOT RUN.

Rescoped 2026-10-20, fifth version of this script - these 2 routes
shipped today (PRs #42, #44) and have never had a single real settlement,
so CDP's Bazaar has no fiche for either yet at all (not stale - entirely
absent: a fiche is only CREATED on a route's first real settlement, never
proactively). The previous 3 targets (token-card, search, fact-check) are
dropped from this run - already indexed and settled by the 2026-10-12
version of this script, nothing stale about them from today's changes.

TARGET_ROUTES (2), both paid via POST (json body), matching the method
each is actually indexed under in /.well-known/x402 (both also have a GET
twin registered at the same price - see app/x402_setup.py's
`_get_twin_path` loop - POST chosen here as the richer, more natural shape
for each route's own payload, same reasoning as search/fact-check above):

- due-diligence: address (wallet-intelligence + default token-risk
  target) + url (agent-health target) - USDC's own contract address and
  this service's own /search route, same real values already published as
  this route's own Bazaar sample input (app/x402_setup.py's
  DUE_DILIGENCE_SAMPLE_INPUT) - not fabricated for this script.
- company-leads: sector + country (required) + city (optional) +
  max_results. UNLIKE every other route this script (or its predecessors)
  has ever targeted, this one has a REAL third-party cost per real call
  (Apify, ~$0.003/lead delivered + a $0.0002 start fee - see
  docs/company-leads-dry-run-report.md) - every other route's upstream
  cost is $0 internal compute. max_results is set to 3, not the route's
  own default of 25, specifically to keep that real cost to roughly
  3 x $0.003 + $0.0002 ~= $0.009 for this one bootstrap call, not a full
  25-lead run (~$0.075) that would add nothing extra toward the only goal
  here (one real settlement, so Bazaar creates the fiche).

Same shape as every previous version of this script (ping-pong funding
B<-A, pay from B, stop cleanly the moment A can no longer cover one more
funding, real price read live from each route's own 402 challenge,
_fundings_needed_for_shortfall rounds up rather than requiring an exact
multiple) - copied rather than imported, same "standalone one-off
artifact" reasoning as every other bootstrap script in this directory.

--dry-run
---------
No real payment (DRY_RUN short-circuits before any signing/broadcast).
Reads the REAL on-chain balance and each route's REAL live price, and
simulates the exact same balance bookkeeping and stop condition as a real
run - reports exactly how many of the 2 would settle with A's current
balance, in priority order, plus a total-fundings/total-cost summary.
"""

from __future__ import annotations

import asyncio
import base64
import json
import math
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

# (path, method) - method is whichever one each route is actually
# Bazaar-registered under in /.well-known/x402 (confirmed live 2026-10-20).
TARGET_ROUTES = [
    ("due-diligence", "POST"),
    ("company-leads", "POST"),
]
TARGET_PATHS = [p for p, _m in TARGET_ROUTES]
assert len(TARGET_PATHS) == 2
assert len(set(TARGET_PATHS)) == 2

FUNDING_PATH = "x402-echo"
FUNDING_PRICE = 0.001  # config.PRICE_X402_ECHO
MAX_RETRIES = 3
RETRY_DELAY_S = 10
POST_SETTLE_PAUSE_S = 8
EPSILON = 1e-9

DRY_RUN = "--dry-run" in sys.argv

# Real sample inputs per route, both used as a json body (POST):
# - due-diligence: the same address/url pair already published as this
#   route's own Bazaar sample (app/x402_setup.py's
#   DUE_DILIGENCE_SAMPLE_INPUT) - USDC's own Base contract, and this
#   service's own /search route as the agent-health audit target.
# - company-leads: a real sector+country+city combination already
#   verified to return real leads (docs/company-leads-dry-run-report.md),
#   max_results capped at 3 (not the route's own default of 25) to bound
#   the real per-call Apify cost this one route alone carries - see the
#   module docstring.
SAMPLE_INPUTS = {
    "due-diligence": {"address": USDC_CONTRACT, "url": f"{BASE_URL}/search"},
    "company-leads": {"sector": "Software Development", "country": "fr", "city": "Paris", "max_results": 3},
}
assert set(SAMPLE_INPUTS) == set(TARGET_PATHS)


async def usdc_balance(address: str) -> float:
    data = "0x70a08231000000000000000000000000" + address[2:].lower()
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(
            BASE_RPC,
            json={"jsonrpc": "2.0", "id": 1, "method": "eth_call", "params": [{"to": USDC_CONTRACT, "data": data}, "latest"]},
            headers={"User-Agent": "bootstrap-bazaar-unindexed-routes/2.0"},
        )
        resp.raise_for_status()
        raw = resp.json().get("result", "0x0")
    return int(raw, 16) / 1_000_000


# --- route + price discovery, both live from the real catalog ---------------

async def fetch_route(path: str, method: str) -> dict:
    """Matches BOTH resource URL and method - several of these paths have
    both a GET and a POST entry in /.well-known/x402 (same URL, different
    extension shape), so matching on URL alone would silently pick
    whichever one happens to come first in the list."""
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(f"{BASE_URL}/.well-known/x402")
        resp.raise_for_status()
        resources = resp.json()["resources"]

    target_url = f"{BASE_URL}/{path}"
    for r in resources:
        if r["resource"].rstrip("/") != target_url or r["method"].upper() != method:
            continue
        bazaar_input = (r.get("extensions") or {}).get("bazaar", {}).get("info", {}).get("input", {})
        if path in SAMPLE_INPUTS:
            sample = SAMPLE_INPUTS[path]
            if method == "GET":
                bazaar_input = {**bazaar_input, "queryParams": sample}
            else:
                bazaar_input = {**bazaar_input, "bodyType": "json", "body": sample}
        # else (e.g. the x402-echo funding route): use the catalog's own
        # default sample as-is, no override needed.
        return {"path": path, "url": r["resource"], "method": method, "bazaar_input": bazaar_input}
    raise RuntimeError(f"route introuvable dans /.well-known/x402: {method} /{path}")


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


# --- payment plumbing, ported from bootstrap_pretrade_base_routes.py -------

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
            return outcome, tx, body_text[:300]
        if outcome == "a_retenter" and attempt < MAX_RETRIES:
            print(f"  {route['path']}: status={status} non-200 (essai {attempt}/{MAX_RETRIES}, corps: {body_text[:100]!r}), nouvel essai dans {RETRY_DELAY_S}s")
            await asyncio.sleep(RETRY_DELAY_S)
            continue
        detail = None if outcome == "reglee" else body_text[:300]
        return outcome, tx, detail

    return "a_retenter", None, "essais epuises"


def _fundings_needed_for_shortfall(shortfall: float) -> int:
    if shortfall <= EPSILON:
        return 0
    return max(1, math.ceil(shortfall / FUNDING_PRICE - EPSILON))


async def fund_b_for_route(account_a, bal_a: float, bal_b: float, price: float, label: str) -> tuple[float, float, bool, int]:
    """Returns (bal_a, bal_b, ok, n_fundings). ok=False means A's balance
    can't cover the funding this ONE route needs - caller should stop
    here, not treat it as a route failure."""
    shortfall = price - bal_b
    n_fundings = _fundings_needed_for_shortfall(shortfall)

    if n_fundings == 0:
        return bal_a, bal_b, True, 0

    cost_to_a = n_fundings * FUNDING_PRICE
    if bal_a + EPSILON < cost_to_a:
        print(f"  {label}: besoin de {n_fundings} financement(s) ({cost_to_a:.6f}) mais A n'a que {bal_a:.6f}")
        return bal_a, bal_b, False, n_fundings

    funding_route = await fetch_route(FUNDING_PATH, "POST")
    for i in range(1, n_fundings + 1):
        outcome, tx, detail = await _pay_with_retry(account_a, funding_route)
        if outcome != "reglee":
            raise RuntimeError(f"echec financement pour {label} ({i}/{n_fundings}): {detail}")
        bal_a -= FUNDING_PRICE
        bal_b += FUNDING_PRICE
        print(f"  financement {i}/{n_fundings} pour {label} regle, tx={tx} | solde local A={bal_a:.6f} B={bal_b:.6f}")
        if not DRY_RUN:
            await asyncio.sleep(POST_SETTLE_PAUSE_S)

    return bal_a, bal_b, True, n_fundings


async def main() -> int:
    bal_a = await usdc_balance(ADDRESS_A)
    bal_b = await usdc_balance(ADDRESS_B)
    print(f"A balance (on-chain): {bal_a:.6f} USDC")
    print(f"B balance (on-chain): {bal_b:.6f} USDC")
    print(f"{len(TARGET_ROUTES)} fiches Bazaar a creer (aucun reglement reel jamais vu sur ces routes), prix reel lu par route, financement a la volee")
    print()

    account_a = account_b = None
    if not DRY_RUN:
        key_a = (os.getenv("KEY_A") or "").strip()
        key_b = (os.getenv("KEY_B") or "").strip()
        if not key_a or not key_b:
            print(
                "KEY_A et/ou KEY_B manquant(e).\n"
                "  read -s KEY_A && export KEY_A && \\\n"
                "    read -s KEY_B && export KEY_B && \\\n"
                "    .venv/bin/python scripts/bootstrap_base_unindexed_routes.py && \\\n"
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

    reached = []
    failed = []
    not_attempted = []
    total_fundings = 0
    total_target_cost = 0.0

    for i, (path, method) in enumerate(TARGET_ROUTES, start=1):
        route = await fetch_route(path, method)
        price = await fetch_price(route)

        bal_a, bal_b, ok, n_fundings = await fund_b_for_route(account_a, bal_a, bal_b, price, path)
        total_fundings += n_fundings
        if not ok:
            not_attempted = TARGET_PATHS[i - 1:]
            print(f"\nARRET: solde A insuffisant pour financer la route {i}/{len(TARGET_ROUTES)} ({path}).")
            break

        outcome, tx, body_text = await _pay_with_retry(account_b, route)
        status_word = "OK" if outcome == "reglee" else f"ECHEC({outcome})"
        print(f"[{i}/{len(TARGET_ROUTES)}] {method} /{path} -> {status_word} tx={tx} prix=${price:.6f} | solde local A={bal_a:.6f} B={bal_b:.6f}")
        if outcome == "reglee":
            bal_b -= price
            total_target_cost += price
            reached.append(path)
        else:
            failed.append((path, outcome, body_text))
        if not DRY_RUN and i < len(TARGET_ROUTES):
            await asyncio.sleep(POST_SETTLE_PAUSE_S)

    print()
    print(f"routes reglees: {len(reached)}/{len(TARGET_ROUTES)}")
    if failed:
        print(f"routes en echec (hors solde): {len(failed)}: {[f[0] for f in failed]}")
    if not_attempted:
        print(f"routes NON tentees (solde A epuise), dans l'ordre restant: {len(not_attempted)}")
        for p in not_attempted:
            print(f"  - {p}")
    print(f"total financements /x402-echo: {total_fundings} (${total_fundings * FUNDING_PRICE:.6f})")
    print(f"total cout cible (routes reglees): ${total_target_cost:.6f}")
    print(f"cout total (financements + cibles): ${total_fundings * FUNDING_PRICE + total_target_cost:.6f}")

    if DRY_RUN:
        print("\n=== DRY RUN - aucun paiement reel ===")
        return 0

    return 1 if (failed or not_attempted) else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
