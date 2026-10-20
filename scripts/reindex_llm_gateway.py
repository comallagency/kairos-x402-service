#!/usr/bin/env python3
"""Trigger a CDP Bazaar catalog refresh of POST /v1/chat/completions -
PREPARED, NOT RUN.

Why this exists (measured directly, 2026-10-05, not assumed)
--------------------------------------------------------------
/v1/chat/completions' Bazaar entry is still serving service_name=
"llm-gateway", tags=["llm","inference","openai-compatible"] and an older
description - all three updated on 2026-10-04 (commit e1ef5113: service_name
-> "chat-completions-api", tags -> ["chat completions","ai inference",
"llm api","pay per token llm","openai compatible"], description opens with
"437 models, no account, no API key..."). Confirmed live on
GET /.well-known/x402 right now, but CDP's cached copy still has
lastUpdated=2026-09-28T08:49:12Z.

Comparing against base/simulate and base/quote (PACK PRE-TRADE BASE,
2026-10-04): their lastUpdated jumped to the exact second of their real
bootstrap settlement that same day. The one thing /v1/chat/completions is
missing since the 2026-10-04 edit is a real settled payment - its own
quality.lastCalledAt is still 2026-09-28, the date of the one real payment
scripts/bootstrap_llm_gateway.py made when this route first launched.
Working hypothesis, not proven (CDP's indexer is closed): the catalog
refresh is tied to a real settlement on that specific resource, not an
independent periodic crawl. One more real payment is the cheapest way to
test that hypothesis and, if true, pick up the current vocabulary at the
same time.

Relationship to scripts/bootstrap_llm_gateway.py
---------------------------------------------------
That script already does exactly this mechanically (B pays one real call,
A funds B via /x402-echo if short) - it is NOT reused by import, same
"standalone one-off artifact" reasoning as every other bootstrap script in
this directory (see bootstrap_pretrade_base_routes.py's own docstring).
Copied here under a name that reflects the actual goal this time
(reindexing an existing, already-launched route) rather than a first
listing. MAX_TOKENS=50 is kept unchanged: checked live against OpenRouter's
real catalog (2026-10-05) and the cheapest paid model today
(mistralai/mistral-nemo) already floors at the $0.001 MIN_SETTLE_USD
minimum even at max_tokens=50 - there is no cheaper real call to make, this
already is "le moins cher possible".

--dry-run
---------
No real payment. Prints the cheapest model, A and B's real on-chain
balances, the ceiling POST /v1/chat/completions would quote for that model
at max_tokens=50, and how many /x402-echo fundings (if any) that would
require - fails loudly (non-zero exit) if that ceiling exceeds 0.002 USDC,
the bound this script was sized for.
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
MAX_TOKENS = 50
MAX_EXPECTED_CEILING_USD = 0.002  # the bound this script was sized for

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
            headers={"User-Agent": "reindex-llm-gateway/1.0"},
        )
        resp.raise_for_status()
        raw = resp.json().get("result", "0x0")
    return int(raw, 16) / 1_000_000


def _is_free_model(model_id: str, prompt_price: float, completion_price: float) -> bool:
    """Mirrors app/handlers/llm_gateway.py::_is_free_model exactly (this
    script runs standalone, outside the container, so it can't import the
    app) - free models are excluded from the gateway, so the cheapest PAID
    model must be picked, not just the cheapest."""
    return prompt_price == 0 or completion_price == 0 or model_id.endswith(":free")


async def cheapest_model_and_ceiling() -> tuple[str, float]:
    """Fetches OpenRouter's real catalog and reimplements
    app/handlers/llm_gateway.py::_price_ceiling_usd's formula directly
    (no import from the deployed app - standalone script) so the dry-run
    check reflects what the live route will actually quote."""
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
        if _is_free_model(m["id"], p, c):
            continue
        priced.append((c, p, m["id"]))
    priced.sort()
    completion_price, prompt_price, model = priced[0]

    prompt_tokens = 5  # "Say OK." - see the sample request this script sends
    markup = 1.10
    min_settle = 0.001
    ceiling = (prompt_tokens * prompt_price + MAX_TOKENS * completion_price) * markup
    ceiling = max(ceiling, min_settle)
    return model, ceiling


# --- funding step, ported from bootstrap_llm_gateway.py / bootstrap_bazaar_full.py

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
    return "a_retenter"  # any other non-200 - transient, retryable


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
    """How many FUNDING_PRICE payments close a shortfall - 0 if B already
    covers the ceiling (don't fund what's already there).

    Rounds UP rather than requiring an exact multiple (bootstrap_llm_gateway.py's
    original version raised if the shortfall wasn't an exact multiple of
    FUNDING_PRICE - fine the first time B's balance is 0, but B now carries a
    real /usr/bin/bash.000108 dust remainder from earlier activity, confirmed live
    2026-10-05, so that assumption no longer holds and will only get less
    true as more real/bootstrap traffic passes through B over time). A small
    leftover dust balance in B after funding is harmless and expected."""
    if shortfall <= EPSILON:
        return 0
    import math

    return math.ceil(shortfall / FUNDING_PRICE)


async def fund_b_if_needed(account_a, bal_a: float, bal_b: float, ceiling: float) -> tuple[float, float]:
    shortfall = ceiling - bal_b
    n_fundings = _fundings_needed_for_shortfall(shortfall)

    if n_fundings == 0:
        print(f"B a deja {bal_b:.6f} >= ceiling {ceiling:.6f}, aucun financement necessaire")
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

async def pay_chat_completion(signer, model: str) -> tuple[int, str | None, str]:
    """Returns (http_status, tx_hash_or_None, outcome_label). Signs against
    the "exact" accepts[] option - x402Client.register + find_matching_
    requirements pick it automatically since ExactEvmClientScheme is the
    only scheme registered here (upto is deliberately not - see module
    docstring in bootstrap_llm_gateway.py for why)."""
    from x402 import x402Client
    from x402.http.clients.httpx import x402HttpxClient
    from x402.mechanisms.evm.exact.register import register_exact_evm_client

    client = x402Client()
    register_exact_evm_client(client, signer=signer, networks=NETWORK)

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
    model, ceiling = await cheapest_model_and_ceiling()
    bal_a = await usdc_balance(ADDRESS_A)
    bal_b = await usdc_balance(ADDRESS_B)

    print(f"cheapest model: {model}")
    print(f"computed ceiling (exact, max_tokens={MAX_TOKENS}): ${ceiling:.6f}")
    print(f"A balance (on-chain): {bal_a:.6f} USDC")
    print(f"B balance (on-chain): {bal_b:.6f} USDC")

    if ceiling > MAX_EXPECTED_CEILING_USD:
        print(
            f"ARRET: ceiling ${ceiling:.6f} exceeds the expected ${MAX_EXPECTED_CEILING_USD} "
            "ceiling this script was written for. Re-check before running for real.",
            file=sys.stderr,
        )
        return 1

    if DRY_RUN:
        shortfall = ceiling - bal_b
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
            "    .venv/bin/python scripts/reindex_llm_gateway.py && \\\n"
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
        bal_a, bal_b = await fund_b_if_needed(account_a, bal_a, bal_b, ceiling)
    except Exception as exc:
        print(f"ARRET: {exc}", file=sys.stderr)
        return 1

    if bal_b + EPSILON < ceiling:
        print(f"ARRET: solde B ({bal_b:.6f}) < ceiling ({ceiling:.6f}) apres financement.", file=sys.stderr)
        return 1

    status, tx_hash, outcome = await pay_chat_completion(account_b, model)
    print(f"POST /v1/chat/completions -> HTTP {status}, tx={tx_hash}, {outcome}")
    print(
        "Rappel: la reprise par CDP de la fiche a jour (si l'hypothese du "
        "module docstring est correcte) n'est pas instantanee - revoir "
        "lastUpdated sur /platform/v2/x402/discovery/search plus tard, pas "
        "immediatement apres ce run."
    )
    return 0 if status == 200 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
