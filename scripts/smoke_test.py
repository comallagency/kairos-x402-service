#!/usr/bin/env python3
"""Smoke test checks, reusable against any base URL.

Run standalone after every deploy, before considering it done - exits
non-zero if any of these doesn't return 200: /.well-known/x402,
/openapi.json, /llms.txt, /. Defaults to the public domain
(DEFAULT_BASE_URL); override with the SMOKE_TEST_BASE_URL env var.

Added 2026-09-28 after a DynamicPrice callable (POST /v1/chat/completions'
compute_ceiling_price) crashed /.well-known/x402 with a 500 -
x402/schemas/helpers.py::parse_money got a function object where it
expected a string. The bug had been live since compute_ceiling_price was
introduced, several deploys earlier; every one of those deploys was
verified only by curling whatever route had just changed, which never
included this one. This script exists so an unrelated route breaking is
caught the same way a changed one already was.

Extended 2026-09-29 with a real headless-render check for GET /admin/live
(see check_admin_live() below): that page went blank after a deploy while
still returning HTTP 200 the whole time - a plain status check could never
have caught it. Root cause was a swallowed JS exception in live.html; see
scripts/live_render_check/check.js and live.html's render()/renderInner()
split from the same fix.

Refactored 2026-09-30 (blue-green safety rework, see deploy.sh) to accept
a base_url parameter instead of a hardcoded module constant: the SAME
check logic now runs twice per deploy - once pre-cutover against the new
container's own internal port (scripts/precutover_check.py), and once
post-cutover, repeatedly, against the real public domain through nginx
(scripts/post_cutover_watch.py). Kept runnable standalone (`python3
scripts/smoke_test.py`) for manual checks, unchanged in behavior when run
that way.
"""

import os
import subprocess
import sys
from pathlib import Path

import httpx

DEFAULT_BASE_URL = "https://x402.agentindex.world"
CHECKS = ["/.well-known/x402", "/openapi.json", "/llms.txt", "/"]

_SCRIPT_DIR = Path(__file__).resolve().parent
LIVE_RENDER_CHECK_JS = _SCRIPT_DIR / "live_render_check" / "check.js"


def check_status_codes(base_url: str) -> list[str]:
    failed = []
    with httpx.Client(timeout=15.0) as client:
        for path in CHECKS:
            try:
                resp = client.get(f"{base_url}{path}")
            except httpx.HTTPError as exc:
                print(f"FAIL {path}: {exc}")
                failed.append(path)
                continue
            status = "OK" if resp.status_code == 200 else "FAIL"
            print(f"{status} {path}: {resp.status_code}")
            if resp.status_code != 200:
                failed.append(path)
    return failed


def check_admin_live(base_url: str) -> list[str]:
    """Headless render check (jsdom, see check.js's own docstring for why
    not a full browser) - does GET /admin/live actually populate itself, or
    does it silently stay stuck on its pre-render placeholder while still
    returning HTTP 200?"""
    user = os.getenv("ADMIN_BASIC_AUTH_USER")
    password = os.getenv("ADMIN_BASIC_AUTH_PASS")
    if not user or not password:
        print("SKIP /admin/live render check: ADMIN_BASIC_AUTH_USER/PASS not set in this environment")
        return []

    result = subprocess.run(
        ["node", str(LIVE_RENDER_CHECK_JS), f"{base_url}/admin/live", user, password],
        capture_output=True, text=True, timeout=30,
    )
    print(result.stdout.strip())
    if result.returncode != 0:
        print(result.stderr.strip(), file=sys.stderr)
        return ["/admin/live (render)"]
    return []


def run_all(base_url: str) -> list[str]:
    failed = check_status_codes(base_url)
    failed += check_admin_live(base_url)
    return failed


def main() -> int:
    base_url = os.getenv("SMOKE_TEST_BASE_URL", DEFAULT_BASE_URL)
    failed = run_all(base_url)

    if failed:
        print(f"\n{len(failed)} check(s) failed: {failed}", file=sys.stderr)
        return 1
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
