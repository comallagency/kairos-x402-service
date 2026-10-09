"""Pre-cutover gate (2026-10-09): every (GET, path) accepted for PAYMENT
(x402_setup.py's GET-twin RouteConfig aliases, built so GET-autopay agents
like Lumière PayCheck get a real 402 instead of FastAPI's bare 405) must
have a REAL FastAPI handler registered for GET on that same path -
otherwise a real buyer's payment settles and FastAPI then returns a bare
404/405 for the actual request: paid, not delivered.

Operator-led audit the same day found 100 of ~107 GET-twin routes broken
this way. Staged rollout: mode 1 (blocking only on the 7 routes fixed
first, warning-only on the other 93) while the backlog was cleared in
batches (jev_classify's 7 shared-helper routes, jev.py's 4, pdf/web-read/
extract/summarize, discover/research/agent-claim/fact-check/jobs/
v1-chat-completions, and the purecalc/base_chain engines fixed generically
- one handler-factory change covering ~70 routes at once). All 100 are
now fixed and verified (real in-process calls, real test suite run) - this
gate is FULL BLOCKING again: any GET-accepting paid path without a real
delivering handler fails the deploy, no exceptions list.
"""

import sys
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent


def run(base_url: str) -> list[str]:
    sys.path.insert(0, str(APP_ROOT))
    from app.main import inner_app as app
    from app.x402_setup import _build_route_configs_uncached

    routes = _build_route_configs_uncached()
    get_paths = sorted(
        {path for method, path in (key.split(" ", 1) for key in routes) if method == "GET"}
    )

    def _leaf_routes(routes_):
        """FastAPI's include_router() in this version keeps a
        `_IncludedRouter` wrapper per call (app.routes has one entry per
        include_router call, not one per path) rather than flattening leaf
        Route objects directly into the parent - the real (path, methods)
        pairs live on each wrapper's `.original_router.routes`."""
        for route in routes_:
            if type(route).__name__ == "_IncludedRouter":
                yield from _leaf_routes(route.original_router.routes)
            else:
                yield route

    registered: dict[str, set[str]] = {}
    for route in _leaf_routes(app.routes):
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if path and methods:
            registered.setdefault(path, set()).update(methods)

    failures = []
    for path in get_paths:
        if "GET" in registered.get(path, set()):
            continue
        failures.append(
            f"GET {path}: accepted for payment but no FastAPI GET handler exists "
            f"- would settle then 404/405, paid and not delivered"
        )

    print(f"Checked {len(get_paths)} GET-accepting paid route(s) for a real delivering handler.")
    return failures


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: check_get_delivery.py <base_url>", file=sys.stderr)
        return 2
    failed = run(sys.argv[1].rstrip("/"))
    for v in failed:
        print(f"FAIL {v}")
    if failed:
        print(f"\n{len(failed)} paid-GET-without-delivery failure(s).", file=sys.stderr)
        return 1
    print("OK: every GET-accepting paid route has a real delivering handler.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
