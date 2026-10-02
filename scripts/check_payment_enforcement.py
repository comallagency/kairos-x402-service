"""Pre-cutover gate (2026-10-02), blocking: EVERY (method, path) listed in
build_route_configs() must answer 402 (or 410 if retired) when called with
no payment, and the body must look like a real x402 challenge - never a
delivered result.

Written after a severe incident the same day: app/capacity.py's module-level
ROUTE_KEYS snapshot (taken at import time, before purecalc/dynamic routes
exist - by its own design, documented since before this incident) got
memoized by a newly-added cache in build_route_configs() and silently
became the PERMANENT route table PaymentMiddlewareASGI was built with -
missing all 102 pure-compute routes and every usine-generated route. They
ran with zero payment enforcement: real results delivered, status="paid"
logged, for completely unpaid requests.

check_paid_upstream.py's runtime scan does NOT catch this class of bug - it
only checks routes whose handler module reaches an external paid upstream
(OpenRouter/Jev), by design, to catch a narrower (but also real) risk. Pure
compute routes have no upstream cost, so they were invisible to that gate.
This script checks the full catalog instead, independent of what any
upstream the handler happens to call.

Run from inside the new container against its own internal port, same
guarantee as the rest of precutover_check.py: a failure here aborts the
deploy before nginx is ever touched.
"""

import sys
from pathlib import Path

import httpx

APP_ROOT = Path(__file__).resolve().parent.parent


def run(base_url: str) -> list[str]:
    sys.path.insert(0, str(APP_ROOT))
    from app.retired_routes import RETIRED_ROUTES
    from app.x402_setup import _build_route_configs_uncached

    routes = _build_route_configs_uncached()
    failed = []
    checked = 0

    with httpx.Client(timeout=15.0) as client:
        for route_key in sorted(routes):
            method, path = route_key.split(" ", 1)
            if method == "HEAD":
                continue
            retired_prefix = next(
                (p for p in RETIRED_ROUTES if path == p or path.startswith(p + "/")), None
            )
            checked += 1
            try:
                resp = (
                    client.get(f"{base_url}{path}")
                    if method == "GET"
                    else client.request(method, f"{base_url}{path}", json={})
                )
            except httpx.HTTPError as exc:
                failed.append(f"{method} {path}: request error {exc}")
                continue

            expect = 410 if retired_prefix else 402
            if resp.status_code != expect:
                failed.append(
                    f"{method} {path}: expected {expect}, got {resp.status_code} "
                    f"- body: {resp.text[:200]}"
                )
                continue

            if expect == 402:
                try:
                    body = resp.json()
                except ValueError:
                    failed.append(f"{method} {path}: 402 but non-JSON body - nothing delivered check failed")
                    continue
                if "x402Version" not in body and "accepts" not in body:
                    failed.append(
                        f"{method} {path}: got 402 but body doesn't look like a real x402 "
                        f"challenge (possible leaked content): {str(body)[:200]}"
                    )

    print(f"Checked {checked} payment-protected (method, path) pair(s) for 402/410, nothing delivered.")
    return failed


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: check_payment_enforcement.py <base_url>", file=sys.stderr)
        return 2
    failed = run(sys.argv[1].rstrip("/"))
    for v in failed:
        print(f"FAIL {v}")
    if failed:
        print(f"\n{len(failed)} payment-enforcement failure(s).", file=sys.stderr)
        return 1
    print("OK: every payment-protected route enforces payment, nothing delivered unpaid.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
