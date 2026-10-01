#!/usr/bin/env python3
"""Bazaar bootstrap for the 51 pure-compute routes added 2026-10-02 -
PREPARED, NOT RUN.

Pay-as-you-go (2026-10-02 rewrite): unlike bootstrap_new_routes.py (funds
B once for the SUM of all targets up front), this funds B via /x402-echo
right before EACH route - same ping-pong dance as bootstrap_token_risk.py,
looped - then pays that one route, before moving to the next. A's real
balance (~$0.02) cannot cover the full $0.102 (51 x $0.002) up front, so
funding per-route lets the script get as many routes live as the balance
actually allows, stopping cleanly the moment A can no longer cover the
next route's funding - printing exactly which routes were reached and
which were not, rather than failing all-or-nothing.

TARGET_PATHS is pre-ordered by priority: for each route, one realistic
agent query was run against CDP's discovery search (top 5 results) and
against our own description, both embedded with nomic-embed-text (local
Ollama, same method as the earlier Bazaar description-optimization batch)
and compared by cosine similarity. Ranked by gap = best_competitor_score -
our_score, descending.

Finding worth flagging before you run this: every one of the 51 gaps came
back NEGATIVE - CDP's top-5 for all 51 queries are dominated by unrelated
crypto/AI-infrastructure services (ERC20 balance checks, block height, AI
chat completions), never a real competitor in these specific niches. There
is currently no real competition for any of these 51 categories on the
Bazaar - the order below reflects "closest topical overlap with whatever
CDP does return" (least-negative gap first), not "most urgently contested
niche," since no niche here is actually contested yet. See the ranking
report for the full table and reasoning before deciding whether to keep
this order as-is.

Funding step, retry logic: identical primitives to
scripts/bootstrap_new_routes.py / bootstrap_token_risk.py - see those
scripts' docstrings. Needs both KEY_A and KEY_B (never logged, never
printed).

--dry-run
---------
No real payment (DRY_RUN short-circuits before any signing/broadcast).
Simulates the exact same balance bookkeeping as a real run and reports
how many of the 51 routes the current on-chain balance would actually
reach, in priority order, before stopping.
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
# (2026-10-02) - gap = best_competitor_score - our_score, descending (most
# negative last). See this script's module docstring for the important
# caveat: every gap is negative, there is no real competitor for any of
# these 51 routes today.
TARGET_PATHS = [
    "validate/vin", "hash/hash", "encoding/transcode", "text/slug", "time/humanize",
    "encoding/detect", "validate/iban", "hash/hmac", "json/diff", "validate/luhn",
    "time/add", "text/diff", "geo/bbox", "time/between", "money/allocate",
    "validate/routing", "stats/percentile", "number/radix", "stats/regression",
    "geo/point-in-polygon", "stats/summary", "geo/geohash", "validate/imei",
    "time/format", "regex/replace", "validate/ean", "geo/distance", "money/format",
    "geo/midpoint", "json/query", "time/convert", "validate/siret", "validate/isbn",
    "number/ordinal", "unit/convert", "fraction/simplify", "time/iso-week",
    "text/readability", "time/business-days", "time/parse", "json/schema-infer",
    "json/flatten", "geo/bearing", "number/words", "regex/test", "number/roman",
    "stats/correlation", "validate/isin", "text/case", "geo/dms", "json/validate",
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
    51 routes and B accumulates small dust residue between them (e.g.
    $0.000108 left over here before the first route even starts) - an
    exact-multiple requirement would halt the whole run on that dust. A
    small positive leftover in B after funding is fine and expected."""
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
    print(f"{len(TARGET_PATHS)} routes en file, prix fixe $0.002 chacune, financement a la volee par route")
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
