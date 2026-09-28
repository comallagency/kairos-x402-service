#!/usr/bin/env python3
"""Post-recreate smoke test.

Run this after every `docker compose up --force-recreate x402`, before
considering a deploy done - and from now on, before it too, against
whatever is still live, so a check that would have failed isn't the thing
that gets replaced. Exits non-zero if any of these doesn't return 200:
/.well-known/x402, /openapi.json, /llms.txt, /.

Added 2026-09-28 after a DynamicPrice callable (POST /v1/chat/completions'
compute_ceiling_price) crashed /.well-known/x402 with a 500 -
x402/schemas/helpers.py::parse_money got a function object where it
expected a string. The bug had been live since compute_ceiling_price was
introduced, several deploys earlier; every one of those deploys was
verified only by curling whatever route had just changed, which never
included this one. This script exists so an unrelated route breaking is
caught the same way a changed one already was.
"""

import sys

import httpx

BASE_URL = "https://x402.agentindex.world"
CHECKS = ["/.well-known/x402", "/openapi.json", "/llms.txt", "/"]


def main() -> int:
    failed = []
    with httpx.Client(timeout=15.0) as client:
        for path in CHECKS:
            try:
                resp = client.get(f"{BASE_URL}{path}")
            except httpx.HTTPError as exc:
                print(f"FAIL {path}: {exc}")
                failed.append(path)
                continue
            status = "OK" if resp.status_code == 200 else "FAIL"
            print(f"{status} {path}: {resp.status_code}")
            if resp.status_code != 200:
                failed.append(path)

    if failed:
        print(f"\n{len(failed)} check(s) failed: {failed}", file=sys.stderr)
        return 1
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
