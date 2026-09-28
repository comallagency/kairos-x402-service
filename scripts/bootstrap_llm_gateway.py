#!/usr/bin/env python3
"""Bazaar bootstrap for POST /v1/chat/completions - PREPARED, NOT RUN.

B pays for one real chat completion against the cheapest PAID model
currently listed by GET /v1/models (by completion price, ties broken by
prompt price; free models - price 0 or a ":free" id suffix, excluded from
the gateway 2026-09-28 - are skipped, see _is_free_model below), with
max_tokens=50, toward A - using the "exact" accepts[] option
(added 2026-09-28 alongside "upto" specifically so a buyer with no ETH and
no Permit2 allowance, like B, isn't excluded). One real settlement is
enough to give the route a live Bazaar listing, same purpose as
bootstrap_bazaar_full.py's ping-pong for the other routes.

Why "exact", not "upto", for this bootstrap
---------------------------------------------
"upto" (Permit2-based) needs the payer to hold ETH (for their own client to
approve Permit2's USDC allowance on-chain) and to have already granted that
allowance - B has neither. "exact" (EIP-3009) needs neither: no prior
approval, no gas paid by the signer, exactly the signing path
bootstrap_bazaar_full.py already uses successfully for every other route.
This script reuses that exact same registration call
(register_exact_evm_client), unlike the abandoned upto-only version of this
script, which used the untested UptoEvmScheme client path.

Funding step (added 2026-09-28)
---------------------------------
Ported from bootstrap_bazaar_full.py's own funding step, same reasoning:
B's balance is 0 (checked live 2026-09-28), A's is ~$0.007 - not enough to
just fund B once for a generous cushion and call it done forever, but
enough to front exactly this route's ceiling, once. If B's local balance
doesn't already cover the computed ceiling, A pays /x402-echo (fixed
payTo=B, $0.001 each) exactly enough times to close that shortfall, with
an 8s pause after each settlement (POST_SETTLE_PAUSE_S - the chain/
facilitator needs a moment before the next payment from the same wallet is
safe to send, per bootstrap_bazaar_full.py's 2026-09-27 incident writeup).
Only then does B pay the actual target route. This is why the script now
needs both KEY_A and KEY_B, not just KEY_B.

--dry-run
---------
No real payment. Prints the cheapest model, A and B's balances, the
ceiling POST /v1/chat/completions would quote for that model at
max_tokens=50, and how many fundings (if any) that would require - and
fails loudly (non-zero exit) if that ceiling exceeds 0.002 USDC, the bound
this script was sized for.
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
            headers={"User-Agent": "bootstrap-llm-gateway/1.0"},
        )
        resp.raise_for_status()
        raw = resp.json().get("result", "0x0")
    return int(raw, 16) / 1_000_000


def _is_free_model(model_id: str, prompt_price: float, completion_price: float) -> bool:
    """Mirrors app/handlers/llm_gateway.py::_is_free_model exactly (this
    script runs standalone, outside the container, so it can't import the
    app) - free models were excluded from the gateway on 2026-09-28, so the
    bootstrap must pick the cheapest PAID model, not just the cheapest."""
    return prompt_price == 0 or completion_price == 0 or model_id.endswith(":free")


async def cheapest_model_and_ceiling() -> tuple[str, float]:
    """Fetches OpenRouter's real catalog and reimplements
    app/handlers/llm_gateway.py::_price_ceiling_usd's formula directly
    (no import from the deployed app - this script runs standalone,
    outside the container) so the dry-run check reflects what the live
    route will actually quote."""
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


# --- funding step, ported from bootstrap_bazaar_full.py --------------------

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
    covers the ceiling (don't fund what's already there)."""
    if shortfall <= EPSILON:
        return 0
    n = round(shortfall / FUNDING_PRICE)
    if n < 1:
        n = 1
    if abs(n * FUNDING_PRICE - shortfall) > 1e-6:
        raise RuntimeError(f"écart {shortfall} n'est pas un multiple exact de {FUNDING_PRICE} - financement impossible")
    return n


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
    only scheme registered here (upto is deliberately not)."""
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
            "    .venv/bin/python scripts/bootstrap_llm_gateway.py && \\\n"
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
    return 0 if status == 200 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
