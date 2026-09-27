#!/usr/bin/env python3
"""Single Bazaar bootstrap for all 20 indexable routes - alternating flow.

Why alternating, not "fund B for everything up front"
-------------------------------------------------------
The first version of this script funded B once for the full sum of all 19
target routes (~$0.023) before paying any of them. That assumes A holds
that much - it doesn't (A has ~$0.004). The money doesn't need to: it can
circulate. For each route, A only ever fronts exactly that route's price
(1 funding payment for $0.001, 2 for $0.002), B immediately pays it back
to A via the route itself, and the total held across A+B never moves -
only its split does, one route at a time.

Flow, per route (ascending by price)
-------------------------------------
1. A pays /x402-echo (fixed payTo=B, $0.001 each) exactly as many times
   as this route's price requires. Each of these payments is itself a
   real settlement on /x402-echo, refreshing its own Bazaar listing.
2. B pays the route once, toward A (fixed payTo=A, unchanged).
3. Repeat for the next route.

Balance tracking - local, not re-read from chain mid-run
-----------------------------------------------------------
Read once from chain before the loop starts, then tracked locally and
updated optimistically after every payment this script itself made.
Never re-read from chain mid-run - the very first attempt failed because
a mid-run chain read returned stale (settlement-lag) data.

2026-09-27 incident: after /search settled cleanly, the next /x402-echo
funding payment (for /discover) came back as an empty `{}` 402 body. The
requests table confirms this was a genuine settlement failure, not a
malformed request on this script's end: app/intent_logging.py logs
status="payment_failed" specifically when "a payment header was attached
but the response was still 402" (a signed payment was submitted and
rejected at settlement, not just an unauthenticated 402 challenge). It
happened well under a second after the *previous* funding payment settled
successfully - consistent with hitting the facilitator/chain again before
the prior payment had time to fully land. Docker's container was recycled
before this investigation (only ~30min of log history survive a
recreate), so the exact facilitator-side rejection reason couldn't be
pulled from application logs - but the timing and status="payment_failed"
both point at the same class of lag this design already guards against
for reads, just not yet for consecutive writes. Hence POST_SETTLE_PAUSE_S
below, and treating any non-200 (not just "execution reverted" text) as
retryable.

--dry-run
---------
Skips every real payment (each "settles" instantly, tx="SIMULATED") and
prints the same balance trace and final table. Starting balances default
to a real one-time chain read, overridable with DRY_RUN_BAL_A /
DRY_RUN_BAL_B for a what-if test.

--skip=slug1,slug2
-------------------
Drop already-settled routes from this run (e.g. --skip=search after a
partial run that got /search done before failing later). Never affects
/x402-echo funding, which always runs as needed.

Per-payment outcome
--------------------
- 200 -> settled, tx noted.
- invalidReason "amount_too_low" -> noted (not expected at these prices,
  handled defensively), NOT retried (it will never succeed) - route
  dropped, run continues, its funding stays spent on B.
- anything else (execution reverted, an empty {} payment_failed body, a
  timeout, ...) -> transient - retry the SAME payment up to MAX_RETRIES
  times, RETRY_DELAY_S apart. Exhausting retries stops the whole run.

Usage
-----
  read -s KEY_A && export KEY_A && \\
    read -s KEY_B && export KEY_B && \\
    .venv/bin/python scripts/bootstrap_bazaar_full.py [--skip=search] && \\
    unset KEY_A KEY_B

  # dry run, no keys needed, no payment sent:
  .venv/bin/python scripts/bootstrap_bazaar_full.py --dry-run

Safety
------
- Refuses to run unless KEY_A derives to ADDRESS_A and KEY_B to ADDRESS_B
  (skipped in --dry-run, which never signs anything).
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
FUNDING_PRICE = 0.001  # config.PRICE_X402_ECHO
TARGET_SLUGS = [
    "search", "news", "can-pay", "probe", "wallet-balance", "gas-price",
    "wallet-intelligence", "agent-health", "pdf", "web-read", "discover",
    "weather", "crypto",
    "decide", "guard", "verify", "rank",
    "extract", "summarize",
]
BASE_URL = "https://x402.agentindex.world"
NETWORK = "eip155:8453"
AMOUNT_TOO_LOW_MARKER = "amount_too_low"
EXECUTION_REVERTED_MARKER = "execution reverted"
MAX_RETRIES = 3
RETRY_DELAY_S = 10
POST_SETTLE_PAUSE_S = 8
EPSILON = 1e-9

USDC_CONTRACT = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
BASE_RPC = "https://mainnet.base.org"

DRY_RUN = "--dry-run" in sys.argv
SKIP_SLUGS = {
    s.strip() for arg in sys.argv if arg.startswith("--skip=")
    for s in arg[len("--skip="):].split(",") if s.strip()
}


# --- Base mainnet USDC balance (read once, at startup, only) ----------------

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
    return "a_retenter"  # any other non-200 (execution reverted, empty {}, etc.) - retryable


def _print_table(results: list[tuple[str, str, str | None, str, str | None]]) -> None:
    print("\n=== resume ===")
    print(f"{'route':26s} {'payeur':44s} {'resultat'}")
    for slug, payer, tx, outcome, detail in results:
        if outcome == "reglee":
            label = f"reglee, tx={tx}"
        elif outcome == "amount_too_low":
            label = "amount_too_low"
        elif outcome == "a_retenter":
            label = f"{MAX_RETRIES} essais epuises, non reglee: {detail}"
        else:
            label = f"autre erreur: {detail}"
        print(f"{slug:26s} {payer:44s} {label}")


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


def _fundings_needed(price: float) -> int:
    n = round(price / FUNDING_PRICE)
    if abs(n * FUNDING_PRICE - price) > EPSILON:
        raise RuntimeError(f"prix {price} n'est pas un multiple exact de {FUNDING_PRICE} - financement impossible")
    return n


async def run(account_a, account_b, funding_route: dict, routes: dict[str, dict], bal_a: float, bal_b: float, results: list) -> tuple[float, float]:
    ordered = sorted(routes.values(), key=lambda r: r["price"])
    print(f"ordre (prix croissant): {[r['slug'] for r in ordered]}")
    print(f"solde local de depart: A={bal_a:.6f} B={bal_b:.6f}\n")

    for route in ordered:
        price = route["price"]
        n_fundings = _fundings_needed(price)
        print(f"--- {route['slug']} (${price:.3f}, {n_fundings} financement(s)) ---")

        for i in range(1, n_fundings + 1):
            if bal_a + EPSILON < FUNDING_PRICE:
                raise RuntimeError(
                    f"solde local A insuffisant ({bal_a:.6f}) pour financer {route['slug']} "
                    f"(financement {i}/{n_fundings})"
                )
            outcome, tx, detail = await _pay_with_retry(account_a, funding_route)
            results.append((FUNDING_SLUG, ADDRESS_A, tx, outcome, detail))
            if outcome != "reglee":
                raise RuntimeError(f"echec financement ({i}/{n_fundings}) pour {route['slug']}: {detail}")
            bal_a -= FUNDING_PRICE
            bal_b += FUNDING_PRICE
            print(f"  financement {i}/{n_fundings} regle, tx={tx} | solde local A={bal_a:.6f} B={bal_b:.6f}")
            if not DRY_RUN:
                print(f"  pause {POST_SETTLE_PAUSE_S}s (laisser la chaine rattraper)")
                await asyncio.sleep(POST_SETTLE_PAUSE_S)

        if bal_b + EPSILON < price:
            raise RuntimeError(f"solde local B insuffisant ({bal_b:.6f}) pour payer {route['slug']} (${price:.3f})")

        outcome, tx, detail = await _pay_with_retry(account_b, route)
        results.append((route["slug"], ADDRESS_B, tx, outcome, detail))
        if outcome == "reglee":
            bal_b -= price
            bal_a += price
            print(f"  {route['slug']:22s} regle, tx={tx} | solde local A={bal_a:.6f} B={bal_b:.6f}")
            if not DRY_RUN:
                print(f"  pause {POST_SETTLE_PAUSE_S}s (laisser la chaine rattraper)")
                await asyncio.sleep(POST_SETTLE_PAUSE_S)
            print()
        elif outcome == "amount_too_low":
            print(f"  {route['slug']:22s} amount_too_low - notee, route abandonnee (le financement deja verse reste sur B)\n")
        else:
            raise RuntimeError(f"erreur sur {route['slug']}: {detail}")

    return bal_a, bal_b


async def main() -> int:
    if SKIP_SLUGS:
        print(f"routes ignorees (deja reglees): {sorted(SKIP_SLUGS)}")

    if DRY_RUN:
        print("=== DRY RUN - aucun paiement reel, aucune cle requise ===\n")
        override_a = os.getenv("DRY_RUN_BAL_A")
        override_b = os.getenv("DRY_RUN_BAL_B")
        if override_a is not None and override_b is not None:
            bal_a, bal_b = float(override_a), float(override_b)
            print(f"soldes de depart (surcharge DRY_RUN_BAL_A/B): A={bal_a:.6f} B={bal_b:.6f}")
        else:
            bal_a = await usdc_balance(ADDRESS_A)
            bal_b = await usdc_balance(ADDRESS_B)
            print(f"soldes de depart (lecture on-chain reelle): A={bal_a:.6f} B={bal_b:.6f}")
        account_a = account_b = None
    else:
        key_a = (os.getenv("KEY_A") or "").strip()
        key_b = (os.getenv("KEY_B") or "").strip()
        if not key_a or not key_b:
            print(
                "KEY_A et/ou KEY_B manquant(e).\n"
                "  read -s KEY_A && export KEY_A && \\\n"
                "    read -s KEY_B && export KEY_B && \\\n"
                "    .venv/bin/python scripts/bootstrap_bazaar_full.py [--skip=search] && \\\n"
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

        bal_a = await usdc_balance(ADDRESS_A)
        bal_b = await usdc_balance(ADDRESS_B)
        print(f"soldes de depart (lecture on-chain, unique): A={bal_a:.6f} B={bal_b:.6f}")

    results: list[tuple[str, str, str | None, str, str | None]] = []
    exit_code = 0
    try:
        funding_route = (await fetch_routes([FUNDING_SLUG]))[FUNDING_SLUG]
        wanted = [s for s in TARGET_SLUGS if s not in SKIP_SLUGS]
        routes = await fetch_routes(wanted)
        bal_a, bal_b = await run(account_a, account_b, funding_route, routes, bal_a, bal_b, results)
        print(f"solde local final: A={bal_a:.6f} B={bal_b:.6f}")
    except Exception as exc:
        print(f"\nARRET: {exc}", file=sys.stderr)
        exit_code = 1

    _print_table(results)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
