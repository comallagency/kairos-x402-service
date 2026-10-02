#!/usr/bin/env python3
"""End-to-end real-payment verification (2026-10-02 incident): pays 3 real
routes with wallet A (one core, one Jev, one pure-compute) to prove the
payment gate accepts a genuine signed x402 payment after today's fixes.

Same library pattern as scripts/bootstrap_purecalc_routes.py /
bootstrap_token_risk.py - nothing new, just a 3-route, read-only-report run.

Usage (KEY_A never logged, never stored - typed interactively):
    read -s KEY_A && export KEY_A && \
      python3 verify_real_payments.py && \
      unset KEY_A
"""
import asyncio
import base64
import json
import os
import sys

import httpx
from eth_account import Account

ADDRESS_A = "0xb3F32bdfe8D07825BC0D7387295aB1D7559BA69d"
BASE_URL = "https://x402.agentindex.world"
NETWORK = "eip155:8453"

TARGETS = [
    ("x402-echo", "core"),
    ("decide", "jev"),
    ("text/slug", "purecalc"),
]


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
        return json.dumps(decoded)[:150]
    except Exception:
        return header[:80] + "..."


async def pay_once(http, route: dict) -> tuple[int, str | None, str]:
    bazaar_input = route["bazaar_input"]
    if route["method"] == "GET":
        resp = await http.get(route["url"], params=(bazaar_input.get("queryParams") or {}))
    else:
        body = bazaar_input.get("body") if bazaar_input.get("bodyType") == "json" else None
        resp = await http.post(route["url"], json=body or {})
    tx_hash = _decode_tx_hash(resp.headers)
    return resp.status_code, tx_hash, resp.text


async def main() -> int:
    key_a = (os.getenv("KEY_A") or "").strip()
    if not key_a:
        print("KEY_A manquant.\n  read -s KEY_A && export KEY_A && python3 verify_real_payments.py && unset KEY_A", file=sys.stderr)
        return 2

    account_a = Account.from_key(key_a)
    if account_a.address.lower() != ADDRESS_A.lower():
        print(f"KEY_A ne correspond pas a A ({account_a.address} != {ADDRESS_A}) - arret.", file=sys.stderr)
        return 2

    from x402 import x402Client
    from x402.http.clients.httpx import x402HttpxClient
    from x402.mechanisms.evm.exact.register import register_exact_evm_client

    client = x402Client()
    register_exact_evm_client(client, signer=account_a, networks=NETWORK)

    results = []
    for path, category in TARGETS:
        route = await fetch_route(path)
        print(f"\n=== {category}: {path} ===")
        try:
            async with x402HttpxClient(client, timeout=60.0) as http:
                status, tx, body_text = await pay_once(http, route)
        except Exception as exc:
            print(f"EXCEPTION: {exc}")
            results.append((category, path, "exception", None))
            continue
        ok = status == 200
        print(f"status={status} tx_hash={tx}")
        print(f"body (first 300 chars): {body_text[:300]}")
        results.append((category, path, "OK" if ok else f"FAIL({status})", tx))

    print("\n=== SUMMARY ===")
    for category, path, outcome, tx in results:
        print(f"{category:10s} {path:15s} {outcome:12s} tx={tx}")

    return 0 if all(r[2] == "OK" for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
