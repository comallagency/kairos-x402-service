"""Pre-cutover gate (2026-10-01), blocking: any route whose handler module
can reach a paid upstream (OpenRouter incl. Groq-pinned calls via
chat_completion/chat_completion_with_fallback, or Jev via ask_jev) must
answer 402 unpaid on every method it registers, or 410 if it is in
RETIRED_ROUTES - never 200, never a bare 404/405/500.

Written the same day two real leaks were found and fixed:
- /llm/gemini-flash and /llm/deepseek: pulled from the catalog but the
  real handler stayed registered and reachable - unlisted-but-still-routed
  fell straight through the payment middleware, executing real paid LLM
  calls for free.
- /summarize/sample, /extract/sample, /translate/sample: each called the
  SAME inner helper as its paid sibling route, so a free "sample" hit a
  live, billed OpenRouter call every time.

Two passes, both run from inside the new container against its own
internal port, before nginx ever sees it - a failure here aborts the
deploy by construction, same guarantee as the rest of precutover_check.py.
"""

import ast
import re
import sys
from pathlib import Path

import httpx

APP_ROOT = Path(__file__).resolve().parent.parent
HANDLERS_DIR = APP_ROOT / "app" / "handlers"

PAID_MARKERS = ("chat_completion(", "chat_completion_with_fallback(", "ask_jev(")

# Paths that are legitimately free and never reach a paid upstream for the
# purpose of this check - static discovery/catalog/ops surfaces, and job
# status/result reads (the paid step is POST /jobs itself, already covered).
FREE_PATH_EXACT = {
    "/", "/health", "/favicon.ico", "/icon.png", "/capabilities",
    "/discovery/resources", "/v1/models", "/robots.txt", "/sitemap.xml",
    "/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc",
    "/agent.json", "/glama.json",
}
FREE_PATH_PREFIXES = ("/.well-known/", "/admin", "/skills/", "/server", "/verified-agents", "/jobs/")


def _module_sources() -> dict[str, str]:
    return {p.name: p.read_text(encoding="utf-8") for p in HANDLERS_DIR.glob("*.py")}


def _function_spans(source: str) -> dict[str, str]:
    tree = ast.parse(source)
    lines = source.splitlines(keepends=True)
    spans = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            spans[node.name] = "".join(lines[node.lineno - 1:node.end_lineno])
    return spans


def find_sample_leak_violations() -> list[str]:
    """Static pass: no *sample* function may call (directly or via a named
    helper in the same module) a function whose body reaches a paid
    marker."""
    violations = []
    for fname, source in _module_sources().items():
        try:
            spans = _function_spans(source)
        except SyntaxError:
            continue
        costly = {name for name, body in spans.items() if any(m in body for m in PAID_MARKERS)}
        if not costly:
            continue
        for name, body in spans.items():
            if "sample" not in name.lower():
                continue
            if any(m in body for m in PAID_MARKERS):
                violations.append(f"app/handlers/{fname}:{name}() calls a paid-upstream marker directly")
                continue
            for helper in costly:
                if helper != name and re.search(rf"\b{re.escape(helper)}\s*\(", body):
                    violations.append(
                        f"app/handlers/{fname}:{name}() calls {helper}(), which reaches a paid upstream"
                    )
    return violations


def _paid_capable_modules() -> set[str]:
    modules = set()
    for fname, source in _module_sources().items():
        if any(m.rstrip("(") in source for m in PAID_MARKERS) or "jev_classify" in source:
            modules.add(fname)
    return modules


def _collect_routes(routes, out):
    for r in routes:
        if hasattr(r, "original_router"):
            _collect_routes(r.original_router.routes, out)
        elif hasattr(r, "routes"):
            _collect_routes(r.routes, out)
        else:
            out.append(r)


def check_runtime_gating(base_url: str) -> list[str]:
    sys.path.insert(0, str(APP_ROOT))
    from app.main import inner_app
    from app.retired_routes import RETIRED_ROUTES
    from app.x402_setup import build_route_configs

    paid_keys = set(build_route_configs().keys())
    paid_modules = _paid_capable_modules()

    flat = []
    _collect_routes(inner_app.routes, flat)

    failed = []
    checked = 0
    with httpx.Client(timeout=15.0) as client:
        for route in flat:
            path = getattr(route, "path", None)
            methods = getattr(route, "methods", None) or set()
            endpoint = getattr(route, "endpoint", None)
            if path is None or endpoint is None:
                continue
            if path.endswith("/sample") or path in FREE_PATH_EXACT or path.startswith(FREE_PATH_PREFIXES):
                continue
            module = getattr(endpoint, "__module__", "")
            fname = module.rsplit(".", 1)[-1] + ".py"
            if fname not in paid_modules:
                continue
            retired_prefix = next((p for p in RETIRED_ROUTES if path == p or path.startswith(p + "/")), None)
            for m in sorted(methods):
                if m == "HEAD":
                    continue
                checked += 1
                try:
                    resp = (
                        client.get(f"{base_url}{path}")
                        if m == "GET"
                        else client.request(m, f"{base_url}{path}", json={})
                    )
                except httpx.HTTPError as exc:
                    failed.append(f"{m} {path}: request error {exc}")
                    continue
                if retired_prefix:
                    expect = 410
                elif f"{m} {path}" in paid_keys:
                    expect = 402
                else:
                    failed.append(
                        f"{m} {path}: handler module {fname} reaches a paid upstream but this "
                        f"(method, path) is in neither build_route_configs() nor RETIRED_ROUTES "
                        f"(got {resp.status_code})"
                    )
                    continue
                if resp.status_code != expect:
                    failed.append(f"{m} {path}: expected {expect}, got {resp.status_code}")
    print(f"Checked {checked} paid-capable (method, path) call(s) for 402/410.")
    return failed


def run(base_url: str) -> list[str]:
    failed = find_sample_leak_violations()
    for v in failed:
        print(f"FAIL {v}")
    if not failed:
        print("OK static scan: no sample/free function reaches a paid-upstream helper.")

    runtime_failed = check_runtime_gating(base_url)
    for v in runtime_failed:
        print(f"FAIL {v}")
    if not runtime_failed:
        print("OK runtime scan: every paid-capable route answers 402/410 unpaid.")

    return failed + runtime_failed
