"""Pre-cutover gate (2026-10-01), blocking: a route present in the current
live catalog must still be present in the new one, UNLESS it is now in
RETIRED_ROUTES (a deliberate, declared retirement - fine, that is the
entire point of that registry). An undeclared disappearance - a route
quietly dropped by a code change, a bad merge, a registry edit without the
matching retired_at - fails the deploy instead of shipping a silent 404
for something a reputation prober still expects to find.

The "before" snapshot is read from the public domain, which is still live
and untouched at precutover time (nginx has not been cut over yet) - not
from the old container directly, so this works the same way whether the
old and new containers are blue/green or whatever slot naming is in use.
"""

from urllib.parse import urlparse

import httpx

PUBLIC_URL = "https://x402.agentindex.world"


def _catalog_paths(base_url: str) -> set[str]:
    with httpx.Client(timeout=15.0) as client:
        resp = client.get(f"{base_url}/.well-known/x402")
        resp.raise_for_status()
        data = resp.json()
    paths = set()
    for resource in data.get("resources", []):
        path = urlparse(resource["resource"]).path
        if path and path != "/":
            paths.add(path)
    return paths


def run(new_base_url: str, public_url: str = PUBLIC_URL) -> list[str]:
    try:
        old_paths = _catalog_paths(public_url)
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        print(f"FAIL reading current live catalog from {public_url}: {exc}")
        return [f"{public_url}/.well-known/x402 (previous-catalog read)"]

    try:
        new_paths = _catalog_paths(new_base_url)
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        print(f"FAIL reading new catalog from {new_base_url}: {exc}")
        return [f"{new_base_url}/.well-known/x402 (new-catalog read)"]

    disappeared = old_paths - new_paths
    if not disappeared:
        print(f"OK catalog completeness: all {len(old_paths)} previously-listed route(s) still present.")
        return []

    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from app.retired_routes import RETIRED_ROUTES

    failed = []
    for path in sorted(disappeared):
        retired = any(path == p or path.startswith(p + "/") for p in RETIRED_ROUTES)
        if retired:
            print(f"OK {path}: no longer in the catalog but correctly in RETIRED_ROUTES (410, declared retirement)")
        else:
            msg = f"{path}: disappeared from the catalog WITHOUT being added to RETIRED_ROUTES - would 404"
            print(f"FAIL {msg}")
            failed.append(msg)
    return failed
