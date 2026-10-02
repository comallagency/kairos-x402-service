#!/usr/bin/env python3
"""Pre-cutover verification (2026-09-30) - run BEFORE nginx ever sends the
new container a single real request.

Runs the full smoke test (scripts/smoke_test.py) plus a real GET
.../sample call for EVERY route currently in the catalog (read live from
the new container's own /.well-known/x402, so a route added or removed in
this same deploy is automatically covered) - all against the new
container's OWN internal address (http://127.0.0.1:8000 from inside the
container itself, via `docker exec`), never through nginx, since nginx
has not been touched yet at this point in deploy.sh.

A single failure here means deploy.sh aborts before nginx is touched at
all: the live container keeps serving every external request throughout,
unaffected, the same as before this check existed. This is what makes
"a broken build never reaches a real user" a guarantee rather than a hope
that happens to hold on smaller changes.

Usage: python3 scripts/precutover_check.py <base_url>
e.g.:  python3 scripts/precutover_check.py http://127.0.0.1:8000
"""

import sys
from urllib.parse import urlparse

import httpx

from smoke_test import run_all  # sibling module; works when run as `python3 scripts/precutover_check.py`
import check_paid_upstream
import check_catalog_completeness
import check_payment_enforcement


def _catalog_sample_paths(base_url: str) -> list[str]:
    with httpx.Client(timeout=15.0) as client:
        resp = client.get(f"{base_url}/.well-known/x402")
        resp.raise_for_status()
        data = resp.json()

    paths: set[str] = set()
    for resource in data.get("resources", []):
        path = urlparse(resource["resource"]).path
        if path and path != "/":
            paths.add(path)
    return sorted(paths)


def check_catalog_samples(base_url: str) -> list[str]:
    failed = []
    try:
        paths = _catalog_sample_paths(base_url)
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        print(f"FAIL reading catalog from {base_url}/.well-known/x402: {exc}")
        return [f"{base_url}/.well-known/x402 (catalog read)"]

    print(f"Checking {len(paths)} catalog route(s) for a working /sample...")
    with httpx.Client(timeout=15.0) as client:
        for path in paths:
            sample_url = f"{base_url}{path}/sample"
            try:
                resp = client.get(sample_url)
            except httpx.HTTPError as exc:
                print(f"FAIL {sample_url}: {exc}")
                failed.append(sample_url)
                continue
            status = "OK" if resp.status_code == 200 else "FAIL"
            print(f"{status} {sample_url}: {resp.status_code}")
            if resp.status_code != 200:
                failed.append(sample_url)
    return failed


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: precutover_check.py <base_url>", file=sys.stderr)
        return 2
    base_url = sys.argv[1].rstrip("/")

    failed = run_all(base_url)
    failed += check_catalog_samples(base_url)
    failed += check_paid_upstream.run(base_url)
    failed += check_catalog_completeness.run(base_url)
    failed += check_payment_enforcement.run(base_url)

    if failed:
        print(f"\nPRE-CUTOVER CHECK FAILED: {len(failed)} check(s): {failed}", file=sys.stderr)
        return 1
    print("\nPre-cutover check passed - safe to cut nginx over.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
