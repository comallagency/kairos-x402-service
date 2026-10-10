#!/usr/bin/env python3
"""Bazaar bootstrap for /token-card + every route confirmed absent from
CDP's own merchant discovery - PREPARED, NOT RUN.

Rescoped 2026-10-10 from the original base/* -only version: compared our
full route catalog (app.x402_setup.build_route_configs(), 118 distinct
paths) against a live GET /platform/v2/x402/discovery/merchant?payTo=
<our main payTo>, paginated (104 resources today). 14 of our paths are
absent. Of those, /x402-echo is EXCLUDED here on purpose: it pays into
config.X402_ECHO_PAY_TO (0x3cedc3Cba4...45Ec, = ADDRESS_B below), a
different wallet from the main merchant payTo this discovery query is
filtered by - it has 205 real settlements and is still structurally
unable to appear in this specific query, so paying it again here would
not fix anything. That is a wallet/business decision (whether x402-echo
should ever be merchant-discoverable under the main payTo), not a fiche
defect, and is out of scope for this script.

TARGET_ROUTES (13, each confirmed 2026-10-10 against requests.db):
  - token-card: 0 real paid rows ever (price just lowered to $0.005).
  - base/call, base/erc721-tokens, base/events, base/nft-metadata,
    base/nft-owner, base/proxy, base/receipt, base/storage: 0 real paid
    rows ever (8 of the original 14 base/* targets from the first version
    of this script - the other 6, estimate-gas/contract/basename/
    erc20-transfers/pending/tx, already settled and are now indexed,
    confirmed present in the live 104).
  - fact-check, tip: 0 real paid rows ever.
  - agent-claim, jobs: exactly 1 real paid row each, both payer=None,
    both timestamped 2026-10-09T18:03:3{6,9} (3 seconds apart) - reads as
    a one-off smoke test, not organic traffic, and neither triggered
    indexing. Included anyway per the "first payment = first indexing"
    precedent (base/simulate, base/quote, and the 6 already-reindexed
    paths above all confirm the general rule) - a second, cleaner
    settlement may still succeed where the smoke test didn't register.

token-card is paid via GET with queryParams (its new Bazaar-registered
method, see app/x402_setup.py's "GET /token-card" entry) - every other
target here via POST (json body), matching the method each route is
actually indexed under in /.well-known/x402.

Same shape as scripts/bootstrap_pretrade_base_routes.py / the original
base_unindexed version (ping-pong funding B<-A, pay from B, stop cleanly
the moment A can no longer cover one more funding, real price read live
from each route's own 402 challenge, _fundings_needed_for_shortfall
rounds up rather than requiring an exact multiple) - copied rather than
imported, same "standalone one-off artifact" reasoning as every other
bootstrap script in this directory.

--dry-run
---------
No real payment (DRY_RUN short-circuits before any signing/broadcast).
Reads the REAL on-chain balance and each route's REAL live price, and
simulates the exact same balance bookkeeping and stop condition as a real
run - reports exactly how many of the 13 would settle with A's current
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
# Bazaar-registered under in /.well-known/x402 (confirmed live 2026-10-10).
TARGET_ROUTES = [
    ("token-card", "GET"),
    ("agent-claim", "POST"),
    ("fact-check", "POST"),
    ("jobs", "POST"),
    ("tip", "POST"),
    ("base/call", "POST"),
    ("base/erc721-tokens", "POST"),
    ("base/events", "POST"),
    ("base/nft-metadata", "POST"),
    ("base/nft-owner", "POST"),
    ("base/proxy", "POST"),
    ("base/receipt", "POST"),
    ("base/storage", "POST"),
]
TARGET_PATHS = [p for p, _m in TARGET_ROUTES]
assert len(TARGET_PATHS) == 13
assert len(set(TARGET_PATHS)) == 13

FUNDING_PATH = "x402-echo"
FUNDING_PRICE = 0.001  # config.PRICE_X402_ECHO
MAX_RETRIES = 3
RETRY_DELAY_S = 10
POST_SETTLE_PAUSE_S = 8
EPSILON = 1e-9

DRY_RUN = "--dry-run" in sys.argv

# Real sample inputs per route - pulled live from each route's own bazaar
# extension on GET /.well-known/x402 (2026-10-10, 8 base/* ones carried
# over unchanged from the original version of this script), not
# fabricated for this script. token-card's is used as queryParams (GET),
# every other one as a json body (POST).
SAMPLE_INPUTS = {
    "token-card": {"address": USDC_CONTRACT},
    "agent-claim": {"url": "https://x402.agentindex.world", "name": "AgentIndex x402"},
    "fact-check": {"claim": "The Eiffel Tower is taller than the Statue of Liberty."},
    "jobs": {"subject": "Example Corp"},
    "tip": {"message": "Keep building agent infrastructure"},
    "base/call": {
        "to": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        "data": "0x313ce567",
    },
    "base/erc721-tokens": {
        "token": "0xdcfeb48770c42a20428f025a69c093155829a11c",
        "wallet": "0xf70da97812CB96acDF810712Aa562db8dfA3dbEF",
    },
    "base/events": {
        "address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        "topics": ["0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"],
        "from_block": 52118214, "to_block": 52118214,
    },
    "base/nft-metadata": {
        "token": "0x217Ec1aC929a17481446a76Ff9B95B9A64f298Cf",
        "token_id": 1,
    },
    "base/nft-owner": {
        "token": "0xdcfeb48770c42a20428f025a69c093155829a11c",
        "token_id": 129,
    },
    "base/proxy": {"address": "0xb125e6687d4313864e53df431d5425969c15eb2f"},
    "base/receipt": {"hash": "0xc2490a8a0aedd1196617a0e52111f82d6059986db7f1713ab23221c91c42f5c4"},
    "base/storage": {
        "address": "0xb125e6687d4313864e53df431d5425969c15eb2f",
        "slot": "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc",
    },
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
    print(f"{len(TARGET_ROUTES)} routes absentes du Bazaar (payTo principal), prix reel lu par route, financement a la volee")
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
