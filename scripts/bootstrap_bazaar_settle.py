#!/usr/bin/env python3
"""Settle the two remaining $0.002 CDP Bazaar routes (pdf, web-read) with a
consolidation top-up when neither wallet alone can afford one.

State this script assumes (2026-09-25, after the previous ping-pong run)
---------------------------------------------------------------------------
9 routes already settled (search, news, can-pay, probe, wallet-balance,
gas-price, wallet-intelligence, x402-echo, agent-health). X402_PAY_TO is
back on A. The $0.002 total is now split $0.001 on A and $0.001 on B - the
straight ping-pong (one side holds it all) no longer applies: neither A nor
B alone covers a $0.002 route, but together they do.

Consolidation top-up
----------------------
For the current target route, read both balances fresh (no local tracking
carried between routes - only two routes here, a live read each time is
cheap and exactly matches what "regroupement" needs to reason about).
  - if one side alone covers the price -> pay normally, that side to the
    other.
  - else if the SUM covers it -> the side with LESS pays x402-echo
    ($0.001) to the side with MORE, topping it up to $0.002; that side then
    pays the target route back to the original sender.
  - else -> stop, insufficient funds even combined.

x402-echo is the only route ever paid a second time (previous run already
settled it once for indexing) - explicitly allowed here, and only here, as
a pure balance-consolidation instrument. Neither pdf nor web-read is ever
paid twice: each is dropped from the queue the moment it settles or comes
back amount_too_low.

Per-payment outcome (route or consolidation, same handling)
--------------------------------------------------------------
- 200 -> settled, tx noted, removed from the queue (route) or unblocks the
  pending route (consolidation).
- invalidReason "amount_too_low" -> noted, route removed (not expected at
  $0.001/$0.002 but handled defensively).
- "execution reverted" (exception or response body) -> treated as transient
  (RPC balance-read lag against a settlement that just landed) - short
  sleep, retry the same step. Not a stop.
- anything else -> the whole run stops immediately.

Whatever happens, the `finally` block always flips X402_PAY_TO back to A,
rebuilds, recreates, and re-polls the live 402 to confirm A before exit.

Usage
-----
  read -s KEY_A && export KEY_A && \\
    read -s KEY_B && export KEY_B && \\
    .venv/bin/python scripts/bootstrap_bazaar_settle.py && \\
    unset KEY_A KEY_B

Safety
------
- Refuses to run unless KEY_A derives to ADDRESS_A and KEY_B to ADDRESS_B.
- Never prints, logs or otherwise surfaces KEY_A or KEY_B.
- SSH to the VPS only ever edits X402_PAY_TO in .env (backed up before every
  write, outside the repo) and recreates x402-app.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import sys
import time

import httpx
from eth_account import Account

ADDRESS_A = "0xb3F32bdfe8D07825BC0D7387295aB1D7559BA69d"
ADDRESS_B = "0x3cedc3Cba49c3809EE46B9bf60da75d6607b45Ec"
TARGET_SLUGS = ["pdf", "web-read"]
CONSOLIDATION_SLUG = "x402-echo"
MAX_PRICE_USDC = 0.002
BASE_URL = "https://x402.agentindex.world"
NETWORK = "eip155:8453"
EXCLUDED_SLUGS = {"discover", "weather", "crypto"}
AMOUNT_TOO_LOW_MARKER = "amount_too_low"
EXECUTION_REVERTED_MARKER = "execution reverted"
MAX_ITERATIONS = 20
RETRY_DELAY_S = 10

SSH_KEY = os.path.expanduser("~/.ssh/hermes_vps_key")
SSH_HOST = "root@169.58.121.36"
REMOTE_APP_DIR = "/opt/x402/app"
REMOTE_ENV_PATH = f"{REMOTE_APP_DIR}/.env"

USDC_CONTRACT = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
BASE_RPC = "https://mainnet.base.org"


# --- VPS control (SSH from this WSL clone) -------------------------------

def _ssh_exec(remote_command: str, timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["ssh", "-i", SSH_KEY, "-o", "ConnectTimeout=10", "-o", "BatchMode=yes", SSH_HOST, remote_command],
        capture_output=True, text=True, timeout=timeout,
    )


def _ssh_pipe(remote_command: str, stdin_text: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["ssh", "-i", SSH_KEY, "-o", "ConnectTimeout=10", "-o", "BatchMode=yes", SSH_HOST, remote_command],
        input=stdin_text, capture_output=True, text=True, timeout=timeout,
    )


_PATCH_ENV_TEMPLATE = """import re
path = "{remote_env_path}"
s = open(path, encoding="utf-8").read()
s = re.sub(r"^X402_PAY_TO=.*$", "X402_PAY_TO={pay_to}", s, flags=re.MULTILINE)
open(path, "w", encoding="utf-8").write(s)
print("patched")
"""


def apply_env_and_recreate(pay_to: str) -> None:
    backup = _ssh_exec(
        "mkdir -p /root/x402-preclaude-backups && "
        f"cp {REMOTE_ENV_PATH} /root/x402-preclaude-backups/.env.bak-ping-pong-$(date +%Y%m%dT%H%M%S)"
    )
    if backup.returncode != 0:
        raise RuntimeError(f"echec backup .env: {backup.stderr[:300]}")

    patch_script = _PATCH_ENV_TEMPLATE.format(remote_env_path=REMOTE_ENV_PATH, pay_to=pay_to)
    push = _ssh_pipe("cat > /tmp/_patch_env.py", patch_script)
    if push.returncode != 0:
        raise RuntimeError(f"echec envoi du patch .env: {push.stderr[:300]}")

    run = _ssh_exec("python3 /tmp/_patch_env.py && rm -f /tmp/_patch_env.py")
    if run.returncode != 0 or "patched" not in run.stdout:
        raise RuntimeError(f"echec application du patch .env: {run.stderr[:300] or run.stdout[:300]}")

    build = _ssh_exec(f"cd {REMOTE_APP_DIR} && docker compose build x402", timeout=300)
    if build.returncode != 0:
        raise RuntimeError(f"echec docker compose build: {build.stderr[:300]}")

    up = _ssh_exec(f"cd {REMOTE_APP_DIR} && docker compose up -d --no-deps --force-recreate x402", timeout=120)
    if up.returncode != 0:
        raise RuntimeError(f"echec docker compose up: {up.stderr[:300]}")


async def wait_for_pay_to(expected: str, timeout_s: int = 150) -> None:
    deadline = time.monotonic() + timeout_s
    async with httpx.AsyncClient(timeout=15.0) as client:
        while time.monotonic() < deadline:
            try:
                resp = await client.get(f"{BASE_URL}/x402-echo")
                pay_to = resp.json().get("accepts", [{}])[0].get("payTo", "")
            except Exception:
                pay_to = ""
            if pay_to.lower() == expected.lower():
                print(f"  402 confirme payTo={pay_to}")
                return
            await asyncio.sleep(5)
    raise RuntimeError(f"payTo n'a jamais affiche {expected} apres {timeout_s}s")


# --- Base mainnet USDC balance ---------------------------------------------

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


async def fetch_eligible_routes() -> list[dict]:
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(f"{BASE_URL}/.well-known/x402")
        resp.raise_for_status()
        resources = resp.json()["resources"]

    by_slug: dict[str, dict] = {}
    for r in resources:
        slug = r["resource"].rstrip("/").rsplit("/", 1)[-1]
        if slug in EXCLUDED_SLUGS:
            continue
        price = _price(r.get("accepts") or [])
        if price is None or price > MAX_PRICE_USDC:
            continue
        current = by_slug.get(slug)
        if current is not None and current["method"] == "GET":
            continue
        by_slug[slug] = {
            "slug": slug,
            "url": r["resource"],
            "method": r["method"].upper(),
            "price": price,
            "bazaar_input": (r.get("extensions") or {}).get("bazaar", {}).get("info", {}).get("input", {}),
        }
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
    if EXECUTION_REVERTED_MARKER in body_text.lower():
        return "execution_reverted"
    return "autre_erreur"


def _print_table(results: list[tuple[str, str, str | None, str, str | None]]) -> None:
    print("\n=== resume ===")
    print(f"{'route':26s} {'payeur':44s} {'resultat'}")
    for slug, payer, tx, outcome, detail in results:
        label = {
            "reglee": f"reglee, tx={tx}",
            "amount_too_low": "amount_too_low",
            "execution_reverted": "execution reverted - non reglee, reportee",
            "autre_erreur": f"autre erreur: {detail}",
        }[outcome]
        print(f"{slug:26s} {payer:44s} {label}")


# --- settlement with consolidation top-up -----------------------------------

async def _attempt_payment(accounts: dict, payer_addr: str, payto_addr: str, route: dict) -> tuple[str, str | None, str | None]:
    from x402 import x402Client
    from x402.http.clients.httpx import x402HttpxClient
    from x402.mechanisms.evm.exact.register import register_exact_evm_client

    apply_env_and_recreate(payto_addr)
    await wait_for_pay_to(payto_addr)

    signer = accounts[payer_addr.lower()]
    client = x402Client()
    register_exact_evm_client(client, signer=signer, networks=NETWORK)
    try:
        async with x402HttpxClient(client, timeout=60.0) as http:
            status, tx, body_text = await pay_once(http, route)
    except Exception as exc:
        if EXECUTION_REVERTED_MARKER in str(exc).lower():
            return "execution_reverted", None, None
        return "autre_erreur", None, str(exc)[:300]

    outcome = _outcome(status, body_text)
    detail = None if outcome == "reglee" else body_text[:300]
    return outcome, tx, detail


async def run_pdf_web_read(account_a: Account, account_b: Account, results: list) -> None:
    accounts = {ADDRESS_A.lower(): account_a, ADDRESS_B.lower(): account_b}
    by_slug = await fetch_eligible_routes()

    missing = [s for s in TARGET_SLUGS if s not in by_slug]
    if missing:
        raise RuntimeError(f"route(s) cible(s) introuvable(s) dans /.well-known/x402: {missing}")
    consolidation_route = by_slug.get(CONSOLIDATION_SLUG)
    if consolidation_route is None:
        raise RuntimeError(f"route de regroupement '{CONSOLIDATION_SLUG}' introuvable")

    unpaid = [by_slug[s] for s in TARGET_SLUGS]
    print(f"routes a regler: {[r['slug'] for r in unpaid]}")
    print(f"route de regroupement disponible: {CONSOLIDATION_SLUG} (${consolidation_route['price']})")

    for iteration in range(1, MAX_ITERATIONS + 1):
        if not unpaid:
            break
        route = unpaid[0]
        bal_a = await usdc_balance(ADDRESS_A)
        bal_b = await usdc_balance(ADDRESS_B)
        print(f"\n--- iteration {iteration}: cible={route['slug']} (${route['price']}) | solde A={bal_a:.6f} B={bal_b:.6f} ---")

        if bal_a >= route["price"]:
            payer, payto = ADDRESS_A, ADDRESS_B
        elif bal_b >= route["price"]:
            payer, payto = ADDRESS_B, ADDRESS_A
        elif bal_a + bal_b >= route["price"]:
            sender, recipient = (ADDRESS_A, ADDRESS_B) if bal_a <= bal_b else (ADDRESS_B, ADDRESS_A)
            print(
                f"  ni A ni B seul ne couvre ${route['price']} (A={bal_a:.6f}, B={bal_b:.6f}) mais la somme oui - "
                f"regroupement: {sender} paie {CONSOLIDATION_SLUG} (${consolidation_route['price']}) vers {recipient}"
            )
            outcome, tx, detail = await _attempt_payment(accounts, sender, recipient, consolidation_route)
            results.append((f"{CONSOLIDATION_SLUG} (regroupement)", sender, tx, outcome, detail))
            if outcome == "reglee":
                print(f"  regroupement reglee, tx={tx}")
                payer, payto = recipient, sender
            elif outcome == "execution_reverted":
                print(f"  regroupement: execution reverted - nouvel essai dans {RETRY_DELAY_S}s")
                await asyncio.sleep(RETRY_DELAY_S)
                continue
            else:
                raise RuntimeError(f"echec du regroupement {CONSOLIDATION_SLUG}: {detail}")
        else:
            raise RuntimeError(f"solde combine insuffisant pour {route['slug']}: A={bal_a:.6f} B={bal_b:.6f}")

        outcome, tx, detail = await _attempt_payment(accounts, payer, payto, route)
        results.append((route["slug"], payer, tx, outcome, detail))
        if outcome == "reglee":
            print(f"  {route['slug']:22s} status=200 tx={tx} payeur={payer}")
            unpaid = unpaid[1:]
        elif outcome == "amount_too_low":
            print(f"  {route['slug']:22s} amount_too_low - notee")
            unpaid = unpaid[1:]
        elif outcome == "execution_reverted":
            print(f"  {route['slug']:22s} execution reverted - nouvel essai dans {RETRY_DELAY_S}s")
            await asyncio.sleep(RETRY_DELAY_S)
        else:
            raise RuntimeError(f"erreur sur {route['slug']}: {detail}")

    if unpaid:
        print(f"\niterations epuisees, jamais reglees: {[r['slug'] for r in unpaid]}", file=sys.stderr)


async def main() -> int:
    key_a = (os.getenv("KEY_A") or "").strip()
    key_b = (os.getenv("KEY_B") or "").strip()
    if not key_a or not key_b:
        print(
            "KEY_A et/ou KEY_B manquant(e).\n"
            "  read -s KEY_A && export KEY_A && \\\n"
            "    read -s KEY_B && export KEY_B && \\\n"
            "    .venv/bin/python scripts/bootstrap_bazaar_settle.py && \\\n"
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

    results: list[tuple[str, str, str | None, str, str | None]] = []
    exit_code = 0
    try:
        await run_pdf_web_read(account_a, account_b, results)
    except Exception as exc:
        print(f"\nARRET: {exc}", file=sys.stderr)
        exit_code = 1
    finally:
        print(f"\n--- restauration finale : X402_PAY_TO -> A ({ADDRESS_A}) ---")
        try:
            apply_env_and_recreate(ADDRESS_A)
            await wait_for_pay_to(ADDRESS_A)
            print("payTo confirme de nouveau sur A.")
        except Exception as exc:
            print(f"ECHEC de la restauration payTo vers A - intervention manuelle requise: {exc}", file=sys.stderr)
            exit_code = 1

    _print_table(results)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
