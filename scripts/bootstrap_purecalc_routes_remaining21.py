#!/usr/bin/env python3
"""Bazaar bootstrap for the 41 REMAINING pure-compute routes (2026-10-02
price split) - PREPARED, NOT RUN.

10 routes (validate/vin, hash/hash, encoding/transcode, text/slug,
time/humanize, encoding/detect, validate/iban, hash/hmac, json/diff,
validate/luhn) are already bootstrapped at $0.002 and removed from
TARGET_PATHS here - this script only covers the 41 never-published-price
routes, now at $0.001 each (see app/purecalc/routes/*.py - never settled,
so dropping the price carries zero reputation risk).

Pay-as-you-go, one funding per route: at $0.001, a single /x402-echo
funding (also $0.001) always exactly covers one route's price when B's
own balance doesn't already - no multi-funding ceiling math needed like
the 10-route $0.002 bootstrap required. Same ping-pong shape as
bootstrap_token_risk.py, looped: fund B (0 or 1 payment) right before
each route, pay that route, move to the next - stopping cleanly and
printing every remaining route the moment A can no longer cover one more
$0.001 funding, rather than failing all-or-nothing.

TARGET_PATHS keeps the priority order from the original 51-route ranking
(CDP discovery search + local-embedding cosine similarity vs our own
description, gap = best_competitor_score - our_score, descending) with
the 10 already-done routes filtered out - see bootstrap_new_routes.py-era
git history for the full ranking table and the finding that every gap was
negative (no real competitor exists yet for any of these niches).

Funding step, retry logic: identical primitives to
scripts/bootstrap_new_routes.py / bootstrap_token_risk.py - see those
scripts' docstrings. Needs both KEY_A and KEY_B (never logged, never
printed).

--dry-run
---------
No real payment (DRY_RUN short-circuits before any signing/broadcast).
Reads the REAL on-chain balance and simulates the exact same balance
bookkeeping as a real run - reports how many of the 41 routes it would
actually reach, in priority order, before stopping. If B's own balance
already covers a route's $0.001, that route needs zero funding and pays
directly from B - the same fund-if-needed logic already does this
(n_fundings computes to 0 when B's balance covers the price).
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

# Priority order from the CDP-search + local-embedding similarity ranking
# (2026-10-02), the 10 already-bootstrapped routes filtered out. See this
# script's module docstring for the caveat: every gap was negative, there
# is no real competitor for any of these routes today.
ALREADY_BOOTSTRAPPED = {
    "validate/vin", "hash/hash", "encoding/transcode", "text/slug", "time/humanize",
    "encoding/detect", "validate/iban", "hash/hmac", "json/diff", "validate/luhn",
    "time/add", "text/diff", "geo/bbox", "time/between", "money/allocate",
    "validate/routing", "stats/percentile", "number/radix", "stats/regression",
    "geo/point-in-polygon", "stats/summary", "geo/geohash", "validate/imei",
    "time/format", "regex/replace", "validate/ean", "geo/distance", "money/format",
    "geo/midpoint", "json/query",
}
# 2026-10-03: 21 routes remaining after the first 20 of the 41-route batch
# settled for real on 2026-10-02 (confirmed via requests.db, payer=B).
TARGET_PATHS = [
    "time/convert", "validate/siret", "validate/isbn",
    "number/ordinal", "unit/convert", "fraction/simplify", "time/iso-week",
    "text/readability", "time/business-days", "time/parse", "json/schema-infer",
    "json/flatten", "geo/bearing", "number/words", "regex/test", "number/roman",
    "stats/correlation", "validate/isin", "text/case", "geo/dms", "json/validate",
]
assert len(TARGET_PATHS) == 21
assert not (set(TARGET_PATHS) & ALREADY_BOOTSTRAPPED)

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


# --- payment plumbing, ported from bootstrap_token_risk.py -------------------

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
    """Ceiling, not exact-multiple: unlike bootstrap_token_risk.py's
    one-shot single-route funding (where B starts clean), this loops over
    many routes and B accumulates small dust residue between them (e.g.
    $0.000108 left over before the first route of the original 51-route
    run) - an exact-multiple requirement would halt the whole run on that
    dust. A small positive leftover in B after funding is fine and
    expected. At the current $0.001 price this almost always resolves to
    0 or 1 funding anyway."""
    import math

    if shortfall <= EPSILON:
        return 0
    return max(1, math.ceil(shortfall / FUNDING_PRICE - EPSILON))


async def fund_b_for_route(account_a, bal_a: float, bal_b: float, price: float, label: str) -> tuple[float, float, bool]:
    """Returns (bal_a, bal_b, ok). ok=False means A's balance can't cover
    the funding this ONE route needs - caller should stop here, not treat
    it as a route failure."""
    shortfall = price - bal_b
    n_fundings = _fundings_needed_for_shortfall(shortfall)

    if n_fundings == 0:
        return bal_a, bal_b, True

    cost_to_a = n_fundings * FUNDING_PRICE
    if bal_a + EPSILON < cost_to_a:
        print(
            f"  {label}: besoin de {n_fundings} financement(s) ({cost_to_a:.6f}) mais A n'a que {bal_a:.6f}"
        )
        return bal_a, bal_b, False

    funding_route = await fetch_route(FUNDING_PATH)
    for i in range(1, n_fundings + 1):
        outcome, tx, detail = await _pay_with_retry(account_a, funding_route)
        if outcome != "reglee":
            raise RuntimeError(f"echec financement pour {label} ({i}/{n_fundings}): {detail}")
        bal_a -= FUNDING_PRICE
        bal_b += FUNDING_PRICE
        print(f"  financement {i}/{n_fundings} pour {label} regle, tx={tx} | solde local A={bal_a:.6f} B={bal_b:.6f}")
        if not DRY_RUN:
            await asyncio.sleep(POST_SETTLE_PAUSE_S)

    return bal_a, bal_b, True


async def main() -> int:
    bal_a = await usdc_balance(ADDRESS_A)
    bal_b = await usdc_balance(ADDRESS_B)
    print(f"A balance (on-chain): {bal_a:.6f} USDC")
    print(f"B balance (on-chain): {bal_b:.6f} USDC")
    print(f"{len(TARGET_PATHS)} routes en file (deja faites: {sorted(ALREADY_BOOTSTRAPPED)}), prix fixe $0.001 chacune, financement a la volee par route")
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

    reached = []
    failed = []
    not_attempted = []

    for i, path in enumerate(TARGET_PATHS, start=1):
        route = await fetch_route(path)
        price = await fetch_price(route)

        bal_a, bal_b, ok = await fund_b_for_route(account_a, bal_a, bal_b, price, path)
        if not ok:
            not_attempted = TARGET_PATHS[i - 1:]
            print(f"\nARRET: solde A insuffisant pour financer la route {i}/{len(TARGET_PATHS)} ({path}).")
            break

        outcome, tx, body_text = await _pay_with_retry(account_b, route)
        status_word = "OK" if outcome == "reglee" else f"ECHEC({outcome})"
        print(f"[{i}/{len(TARGET_PATHS)}] POST /{path} -> {status_word} tx={tx} prix=${price:.6f} | solde local A={bal_a:.6f} B={bal_b:.6f}")
        if outcome == "reglee":
            bal_b -= price
            reached.append(path)
        else:
            failed.append((path, outcome, body_text))
        if not DRY_RUN and i < len(TARGET_PATHS):
            await asyncio.sleep(POST_SETTLE_PAUSE_S)

    print()
    print(f"routes reglees: {len(reached)}/{len(TARGET_PATHS)}")
    if failed:
        print(f"routes en echec (hors solde): {len(failed)}: {[f[0] for f in failed]}")
    if not_attempted:
        print(f"routes NON tentees (solde A epuise), dans l'ordre de priorite restant: {len(not_attempted)}")
        for p in not_attempted:
            print(f"  - {p}")

    if DRY_RUN:
        print("\n=== DRY RUN - aucun paiement reel ===")
        return 0

    return 1 if (failed or not_attempted) else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
