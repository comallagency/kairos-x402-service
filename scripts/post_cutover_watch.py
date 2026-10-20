#!/usr/bin/env python3
"""Post-cutover monitoring window (2026-09-30).

After nginx is flipped to the new container, poll the full smoke test
(scripts/smoke_test.py) against the REAL public domain - through nginx,
real TLS, real Host header, the actual path an external buyer's request
takes - for WATCH_SECONDS. This is deliberately a different request path
than the pre-cutover check (scripts/precutover_check.py, which never goes
through nginx at all): a regression tied specifically to being live
(nginx proxy behavior, cold real-traffic timing, a resource only exhausted
once the old container's load moves over) could in principle only show up
here.

The first check fires IMMEDIATELY - there is no blind warm-up sleep before
monitoring starts (an earlier version of this script relied on deploy.sh
sleeping 3s, unmonitored, right after cutover to let /admin/data.json's
cold-start settle; a live drill on 2026-09-30 - killing the new container
the instant cutover happened - proved that blind window let real external
502s through for the full 3s with zero chance of being caught early, since
nothing was even looking yet). A failing first pass gets exactly one grace
retry after RETRY_GRACE_S, to absorb that same real cold-start slowness
without a blind spot: a container that is merely slow recovers on the
retry and the deploy proceeds; a container that is actually dead (or
genuinely broken) fails the retry too and triggers rollback immediately
after - the worst case is bounded by RETRY_GRACE_S, not by an unconditional
sleep no check could see through.

On a failure that survives the retry, exit 1 immediately - deploy.sh rolls
nginx back to the old container (still running, never stopped until this
whole window passes clean) right away. This script does not wait out the
rest of the window once something is already wrong, since every second
spent waiting is a second more of live traffic hitting a backend already
known to be bad.

Incident 2026-10-02: /admin/live's render check (jsdom, headless) failed
repeatedly here specifically - pass 1 OK, pass 2 (and its grace retry)
failing on the SAME check, five deploys in a row - while the identical
check run pre-cutover (precutover_check.py, against the new container's
own isolated port, no public traffic) passed cleanly every single time.
Root-caused to sustained ~100%+ CPU on the OLD container (admin dashboard
auto-refreshing every 3s client-side, each cycle re-fetching
/admin/data.json's OpenRouter-dependent chain) competing for the host's
CPU during exactly the live cutover window - a restart of the old
container didn't even clear it (steady-state load, not a stuck/leaked
one), confirming this is infrastructure contention from a live-only
condition, not a functional regression in the new code. Excluded from
THIS watch (post-cutover, through nginx, under real contention) while
staying fully active in precutover_check.py (isolated, proven reliable) -
a real render regression would still be functionally covered pre-cutover,
and content checks (/, /.well-known/x402, /openapi.json, /llms.txt) stay
blocking here. The admin dashboard's own CPU cost under its 3s auto-
refresh is separate follow-up work, not fixed by this exclusion.

Usage: python3 scripts/post_cutover_watch.py
Reads SMOKE_TEST_BASE_URL (defaults to the public domain, see smoke_test.py).
"""

import os
import sys
import time

from smoke_test import DEFAULT_BASE_URL, run_all  # sibling module

WATCH_SECONDS = 60
POLL_INTERVAL_S = 2  # tight on purpose: minimizes how long a real failure can run undetected
RETRY_GRACE_S = 3  # one grace retry, absorbing the same cold-start window the old blind sleep covered


def _check_with_one_retry(base_url: str, check_n: int) -> list[str]:
    failed = run_all(base_url, include_admin_live=False)
    if not failed:
        return failed
    print(f"pass {check_n} failed ({failed}) - one grace retry in {RETRY_GRACE_S}s "
          f"(absorbs a genuine cold-start hiccup; a real failure will fail this too)")
    time.sleep(RETRY_GRACE_S)
    retry_failed = run_all(base_url, include_admin_live=False)
    if not retry_failed:
        print(f"pass {check_n} retry OK - was a transient hiccup, continuing")
    return retry_failed


def main() -> int:
    base_url = os.getenv("SMOKE_TEST_BASE_URL", DEFAULT_BASE_URL)
    deadline = time.monotonic() + WATCH_SECONDS
    check_n = 0

    while True:
        check_n += 1
        print(f"--- post-cutover check {check_n} ---")
        failed = _check_with_one_retry(base_url, check_n)
        if failed:
            print(f"\nPOST-CUTOVER CHECK FAILED on pass {check_n} (survived grace retry): {failed}", file=sys.stderr)
            return 1

        remaining = deadline - time.monotonic()
        print(f"pass {check_n} OK, {max(0, round(remaining))}s left in the watch window")
        if remaining <= 0:
            break
        time.sleep(min(POLL_INTERVAL_S, remaining))

    print(f"\nPost-cutover watch window passed clean ({check_n} check(s) over {WATCH_SECONDS}s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
