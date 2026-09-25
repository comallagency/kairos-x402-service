#!/usr/bin/env python3
"""Bootstrap CDP Bazaar indexing via a ping-pong self-settle between two
wallets on Base mainnet.

Why ping-pong, not a plain self-pay
-------------------------------------
Three things learned the hard way (2026-09-25):
  1. Amounts below $0.001 (search/wallet-balance/gas-price/x402-echo used to
     be $0.0001 or $0.000001) are rejected by the CDP facilitator with
     invalidReason "amount_too_low" - a facilitator floor. Fixed at the
     source: those four PRICE_* constants are now $0.001 in app/config.py.
  2. A genuine self-pay (payer == payTo, same address) is rejected outright
     with "self_send_not_allowed" - CDP will not settle a transfer to itself.
  3. Re-querying the on-chain balance right after a settlement is unreliable:
     the previous transfer is not always visible yet by the time the next
     eth_call lands (RPC read lag vs. facilitator settlement), which showed
     up as spurious "execution reverted" on a payment that should have been
     affordable. Fixed by never deciding on a live re-read (see below).

Fix: two real wallets, A (the live payTo) and B (funded by A's first
ping-pong run: weather + crypto already settled to it). X402_PAY_TO on the
server is flipped between them and whichever wallet currently HOLDS the USDC
pays - so every settlement is a real transfer between two different
addresses, never a self-send. The CDP Facilitator covers gas both ways.

Local balance bookkeeping, not live re-reads
-----------------------------------------------
At the START of each holding session (right after flipping payTo and
confirming it), the holder's on-chain balance is read ONCE. From then on,
every successful (200) payment subtracts that route's price from this local
running total - the actual chain is never re-queried mid-session to decide
whether the NEXT payment is affordable. Flip to the other holder as soon as
the next unpaid route's price would exceed what is locally left.

If a payment nonetheless comes back as "execution reverted" (the RPC-lag
scenario above), it is NOT treated as a stop condition: the route is put
back for a later session, the local balance for the rest of THIS session is
treated as exhausted (no further attempts this session - the same class of
failure would likely repeat), and the loop flips immediately.

Loop
----
  holder = B (USDC is currently there), other = A
  while there are still unpaid eligible routes and iterations remain:
    - flip X402_PAY_TO on the VPS to `other` (SSH from this WSL clone),
      rebuild + recreate x402-app, poll the live 402 until it echoes `other`
    - read `holder`'s on-chain balance once for this session
    - `holder`'s key pays each remaining unpaid route once, cheapest first,
      as long as the LOCAL running balance covers that route's price
    - swap holder/other, repeat

Eligible routes: price <= $0.002, /discover excluded (already indexed
25/09), /weather and /crypto excluded (already settled in the first
ping-pong run). Method/price/example body for every route are read live from
GET /.well-known/x402 - never hand-copied (same rule as the rest of this
project, see app/mpp_middleware.py). GET preferred over POST when a route
exposes both, so a slug is never paid twice under two different methods.

Per-payment outcome
--------------------
- 200 -> settled, tx noted, local balance debited, route removed.
- invalidReason "amount_too_low" (string search in the response body,
  independent of exact JSON shape) -> noted, route removed, never retried.
- "execution reverted" (in an exception message or in a non-200 body) ->
  route kept for later, this session ends early, loop flips. Not a stop.
- anything else -> the WHOLE run stops immediately.

Whatever happens - full success, a stopped run, a broken SSH step - the
`finally` block always flips X402_PAY_TO back to A, rebuilds, recreates, and
re-polls the live 402 to confirm A is back before the script exits.

Usage
-----
  read -s KEY_A && export KEY_A && \\
    read -s KEY_B && export KEY_B && \\
    .venv/bin/python scripts/bootstrap_bazaar_settle.py && \\
    unset KEY_A KEY_B

Safety
------
- Refuses to run unless KEY_A derives to ADDRESS_A and KEY_B derives to
  ADDRESS_B exactly.
- Never prints, logs or otherwise surfaces KEY_A or KEY_B.
- SSH to the VPS only ever edits X402_PAY_TO / MECHANICAL_WALLETS in .env
  (backed up before every write, outside the repo) and recreates x402-app.
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
MAX_PRICE_USDC = 0.002
BASE_URL = "https://x402.agentindex.world"
NETWORK = "eip155:8453"
EXCLUDED_SLUGS = {"discover", "weather", "crypto"}
AMOUNT_TOO_LOW_MARKER = "amount_too_low"
EXECUTION_REVERTED_MARKER = "execution reverted"
MAX_ITERATIONS = 20

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
{mechanical_block}
open(path, "w", encoding="utf-8").write(s)
print("patched")
"""

_MECHANICAL_BLOCK_TEMPLATE = """m = re.search(r"^MECHANICAL_WALLETS=(.*)$", s, re.MULTILINE)
current = m.group(1) if m else ""
wallets = [w.strip() for w in current.split(",") if w.strip()]
if "{addr}".lower() not in [w.lower() for w in wallets]:
    wallets.append("{addr}")
s = re.sub(r"^MECHANICAL_WALLETS=.*$", "MECHANICAL_WALLETS=" + ",".join(wallets), s, flags=re.MULTILINE)
"""


def apply_env_and_recreate(pay_to: str, mechanical_wallet_to_add: str | None = None) -> None:
    backup = _ssh_exec(
        "mkdir -p /root/x402-preclaude-backups && "
        f"cp {REMOTE_ENV_PATH} /root/x402-preclaude-backups/.env.bak-ping-pong-$(date +%Y%m%dT%H%M%S)"
    )
    if backup.returncode != 0:
        raise RuntimeError(f"echec backup .env: {backup.stderr[:300]}")

    mech_block = _MECHANICAL_BLOCK_TEMPLATE.format(addr=mechanical_wallet_to_add) if mechanical_wallet_to_add else ""
    patch_script = _PATCH_ENV_TEMPLATE.format(
        remote_env_path=REMOTE_ENV_PATH, pay_to=pay_to, mechanical_block=mech_block
    )

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


# --- Base mainnet USDC balance (read once per holding session) ------------

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


# --- route discovery (live, never hand-copied) ----------------------------

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

    routes = sorted(by_slug.values(), key=lambda r: r["price"])
    return routes


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
    return "autre_erreur"


def _print_table(results: list[tuple[str, str, str | None, str, str | None]]) -> None:
    print("\n=== resume ===")
    print(f"{'route':22s} {'payeur':44s} {'resultat'}")
    for slug, payer, tx, outcome, detail in results:
        label = {
            "reglee": f"reglee, tx={tx}",
            "amount_too_low": "amount_too_low",
            "execution_reverted": "execution reverted - non reglee, reportee",
            "autre_erreur": f"autre erreur: {detail}",
        }[outcome]
        print(f"{slug:22s} {payer:44s} {label}")


# --- ping-pong loop --------------------------------------------------------

async def run_ping_pong(account_a: Account, account_b: Account, results: list) -> None:
    from x402 import x402Client
    from x402.http.clients.httpx import x402HttpxClient
    from x402.mechanisms.evm.exact.register import register_exact_evm_client

    accounts = {ADDRESS_A.lower(): account_a, ADDRESS_B.lower(): account_b}
    unpaid = await fetch_eligible_routes()
    print(f"{len(unpaid)} route(s) eligible(s) (prix <= ${MAX_PRICE_USDC}, hors discover/weather/crypto):")
    for r in unpaid:
        print(f"  {r['slug']:22s} {r['method']:4s} ${r['price']}")

    # L'USDC est actuellement sur B (weather+crypto deja regles vers B lors
    # du run precedent) - premiere bascule vers A pour que B puisse payer.
    holder, other = ADDRESS_B, ADDRESS_A
    first_flip = True

    for iteration in range(1, MAX_ITERATIONS + 1):
        if not unpaid:
            break
        print(f"\n--- iteration {iteration}: bascule payTo -> {other}, paie {holder} ---")
        apply_env_and_recreate(other, mechanical_wallet_to_add=ADDRESS_B if first_flip else None)
        first_flip = False
        await wait_for_pay_to(other)

        local_balance = await usdc_balance(holder)
        print(f"  solde de depart pour {holder}: {local_balance:.6f} (lu une fois, plus jamais re-interroge ce tour)")

        signer = accounts[holder.lower()]
        client = x402Client()
        register_exact_evm_client(client, signer=signer, networks=NETWORK)

        remaining_routes: list[dict] = []
        progressed = False
        reverted_this_session = False
        async with x402HttpxClient(client, timeout=60.0) as http:
            for route in unpaid:
                if reverted_this_session or route["price"] > local_balance:
                    remaining_routes.append(route)
                    continue

                try:
                    status, tx, body_text = await pay_once(http, route)
                except Exception as exc:
                    if EXECUTION_REVERTED_MARKER in str(exc).lower():
                        print(f"  {route['slug']:22s} execution reverted - solde traite comme epuise, bascule")
                        results.append((route["slug"], holder, None, "execution_reverted", None))
                        remaining_routes.append(route)
                        reverted_this_session = True
                        progressed = True
                        continue
                    results.append((route["slug"], holder, None, "autre_erreur", str(exc)[:300]))
                    raise RuntimeError(f"erreur inattendue sur {route['slug']}: {exc}")

                if status != 200 and EXECUTION_REVERTED_MARKER in body_text.lower():
                    print(f"  {route['slug']:22s} execution reverted - solde traite comme epuise, bascule")
                    results.append((route["slug"], holder, None, "execution_reverted", None))
                    remaining_routes.append(route)
                    reverted_this_session = True
                    progressed = True
                    continue

                outcome = _outcome(status, body_text)
                if outcome == "reglee":
                    print(f"  {route['slug']:22s} status=200 tx={tx}")
                    results.append((route["slug"], holder, tx, "reglee", None))
                    local_balance -= route["price"]
                    progressed = True
                elif outcome == "amount_too_low":
                    print(f"  {route['slug']:22s} amount_too_low - notee")
                    results.append((route["slug"], holder, None, "amount_too_low", None))
                    progressed = True
                else:
                    results.append((route["slug"], holder, None, "autre_erreur", body_text[:300]))
                    raise RuntimeError(f"erreur {status} sur {route['slug']}: {body_text[:300]}")

        unpaid = remaining_routes
        holder, other = other, holder
        if not progressed and unpaid:
            print("  aucun progres ce tour et routes restantes - arret de la boucle.", file=sys.stderr)
            break

    if unpaid:
        print(f"\niterations epuisees ou plus de progres possible, jamais tentees: {[r['slug'] for r in unpaid]}", file=sys.stderr)


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
        await run_ping_pong(account_a, account_b, results)
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
