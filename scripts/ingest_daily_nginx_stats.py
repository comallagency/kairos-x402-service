#!/usr/bin/env python3
"""Daily nginx-log ingestion for GET /admin/daily (2026-09-29).

Runs directly on the VPS HOST (not in the x402-app container) - nginx's
x402.kairos.log lives on the host filesystem only, and the sqlite DB is
equally host-reachable at the path below (the same file the container has
bind-mounted at /app/data/requests.db via docker-compose.yml's
`./data:/app/data`), so there is no need to go through docker exec at all.

Why this exists rather than reusing the app's own `requests` table: that
table only ever gets a row for a request that matched a REGISTERED route
(see IntentLoggingMiddleware) - a genuine unmatched-path 404, or a 502/504
from nginx being unable to reach the backend at all during an outage, never
reaches it. nginx's own access log is the only place either of those is
visible, which is exactly what GET /admin/daily's route-level 404/5xx view
and "requetes totales" need.

Standalone stdlib only (sqlite3, re, no app import) - deliberately does not
share app/db.py's connection machinery, since this runs as a plain cron
process outside the container's Python environment.

Usage:
    python3 ingest_daily_nginx_stats.py [YYYY-MM-DD]
        (defaults to "yesterday" in UTC - the cron's normal daily use)
"""

from __future__ import annotations

import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_PATH = Path("/opt/x402/app/data/requests.db")
LOG_PATHS = [
    Path("/var/log/nginx/x402.kairos.log"),
    Path("/var/log/nginx/x402.kairos.log.1"),
]
HOST = "x402.agentindex.world"
VPS_PUBLIC_IP = "169.58.121.36"  # excluded from distinct_ips, same as db.py's history_7d()

# The 5 probes named in the agenteconomy.report thread - "notateurs/sondes".
PROBER_PATTERNS = {
    "AgentEconomyReport": re.compile(r"agenteconomy|tiagscovik", re.IGNORECASE),
    "Lumiere": re.compile(r"lumiere|lumi.re", re.IGNORECASE),
    "vet402": re.compile(r"vet402", re.IGNORECASE),
    "ApisTrust": re.compile(r"apistrust", re.IGNORECASE),
    "Nitrograph": re.compile(r"nitrograph", re.IGNORECASE),
}

# Generic catalogs/indexers/scanners - NOT one of the 5 named reputation
# probes above, but also not a real visitor. Deliberately narrower than
# db.py's _SCANNER_UA_PATTERNS (that list is tuned to exclude noise from a
# single "distinct_identities" count; this one is a display bucket, so a
# false "indexer" hides a real visitor from the "unknown" bucket instead of
# just under-counting - kept intentionally short and specific).
INDEXER_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"x402-list", r"forgemesh", r"x402scan|poncho", r"402index",
        r"autonomous-directory", r"directory.?probe", r"ampersend",
        r"nohumans", r"uptime|pingdom|healthcheck|monitor",
        r"BrickBlueBot", r"GolemreachTrustBot", r"SERankingBacklinksBot",
        r"TalandorBot", r"ProofBench", r"aisec-registry", r"heldfast",
        r"agent-tools\.cloud-crawler", r"mcp-observatory", r"rokmcp-collector",
        r"\bcrawler\b", r"\bscanner\b", r"\bindex(er|ing)\b",
        r"coinbase.*crawl", r"circle.*scan",
    )
]

MECHANICAL_WALLETS_ENV_KEY = "MECHANICAL_WALLETS"


def _load_mechanical_wallets() -> set[str]:
    env_path = Path("/opt/x402/app/.env")
    if not env_path.exists():
        return set()
    for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line.startswith(f"{MECHANICAL_WALLETS_ENV_KEY}="):
            raw = line.split("=", 1)[1].strip().strip('"').strip("'")
            return {w.strip().lower() for w in raw.split(",") if w.strip()}
    return set()


def classify(ua: str) -> tuple[str, str | None]:
    for name, pattern in PROBER_PATTERNS.items():
        if pattern.search(ua):
            return "prober", name
    for pattern in INDEXER_PATTERNS:
        if pattern.search(ua):
            return "indexer", None
    return "unknown", None


def status_bucket(status: int) -> str:
    if 200 <= status < 300:
        return "c_2xx"
    if status == 402:
        return "c_402"
    if status == 404:
        return "c_404"
    if 400 <= status < 500:
        return "c_4xx_other"
    if status >= 500:
        return "c_5xx"
    return "c_4xx_other"  # 1xx/3xx are not expected on this API-only vhost


def percentile(sorted_vals: list[float], pct: float) -> float | None:
    if not sorted_vals:
        return None
    idx = min(len(sorted_vals) - 1, int(round(pct * (len(sorted_vals) - 1))))
    return round(sorted_vals[idx] * 1000, 1)  # nginx $request_time is seconds -> ms


def main() -> int:
    if len(sys.argv) > 1:
        target_date = sys.argv[1]
    else:
        target_date = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")

    route_agg: dict[str, dict] = defaultdict(lambda: {
        "requests": 0, "c_2xx": 0, "c_402": 0, "c_404": 0, "c_4xx_other": 0, "c_5xx": 0,
        "ips": set(), "latencies": [],
    })
    visitor_agg: dict[str, dict] = defaultdict(lambda: {"count": 0, "ua": "", "ips_seen": False})

    matched_lines = 0
    for log_path in LOG_PATHS:
        if not log_path.exists():
            continue
        with open(log_path, "r", errors="replace") as f:
            for line in f:
                parts = line.rstrip("\n").split("\t")
                if len(parts) < 9:
                    continue
                ts, host, ip, method, uri, status, _bytes, req_time, ua = parts[:9]
                if host != HOST or not ts.startswith(target_date):
                    continue
                try:
                    status_i = int(status)
                except ValueError:
                    continue
                matched_lines += 1
                route = uri.split("?", 1)[0]

                bucket = status_bucket(status_i)
                agg = route_agg[route]
                agg["requests"] += 1
                agg[bucket] += 1
                agg["ips"].add(ip)
                try:
                    agg["latencies"].append(float(req_time))
                except ValueError:
                    pass

                v = visitor_agg[ip]
                v["count"] += 1
                v["ua"] = ua  # last UA wins; good enough for classification display

    if matched_lines == 0:
        print(f"no nginx log lines found for {target_date} (host={HOST}) - nothing to ingest")
        return 0

    route_rows = []
    for route, agg in route_agg.items():
        agg["latencies"].sort()
        route_rows.append({
            "route": route,
            "requests": agg["requests"],
            "c_2xx": agg["c_2xx"],
            "c_402": agg["c_402"],
            "c_404": agg["c_404"],
            "c_4xx_other": agg["c_4xx_other"],
            "c_5xx": agg["c_5xx"],
            "distinct_ips": len(agg["ips"]),
            "p50_ms": percentile(agg["latencies"], 0.50),
            "p95_ms": percentile(agg["latencies"], 0.95),
        })

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # Buyers that day, from the app's own requests table (payer present, not
    # mechanical) - used only to tag daily_visitors.class='buyer'; the
    # canonical buyer LIST with wallet/route/amount for the detail view
    # still comes from app/db.py::day_detail() against chain_payments.
    mechanical = _load_mechanical_wallets()
    day_start = f"{target_date}T00:00:00"
    day_end = f"{target_date}T23:59:59.999999"
    cur = conn.execute(
        "SELECT DISTINCT client_ip, payer FROM requests "
        "WHERE ts >= ? AND ts <= ? AND status='paid' AND client_ip IS NOT NULL",
        (day_start, day_end),
    )
    buyer_ips = {
        r["client_ip"] for r in cur.fetchall()
        if (r["payer"] or "").strip().lower() and (r["payer"] or "").strip().lower() not in mechanical
    }

    ips = list(visitor_agg.keys())
    placeholders = ",".join("?" for _ in ips) if ips else ""
    first_seen: dict[str, str] = {}
    if ips:
        cur = conn.execute(
            f"SELECT ip, MIN(date) AS first_date FROM daily_visitors WHERE ip IN ({placeholders}) GROUP BY ip",
            ips,
        )
        first_seen = {r["ip"]: r["first_date"] for r in cur.fetchall()}

    visitor_rows = []
    for ip, v in visitor_agg.items():
        if ip in buyer_ips:
            cls, prober_name = "buyer", None
        else:
            cls, prober_name = classify(v["ua"])
        visitor_rows.append({
            "ip": ip,
            "user_agent": v["ua"],
            "request_count": v["count"],
            "class": cls,
            "prober_name": prober_name,
            "is_new": ip not in first_seen,
        })

    with conn:
        for r in route_rows:
            conn.execute(
                """INSERT OR REPLACE INTO daily_route_stats
                   (date, route, requests, c_2xx, c_402, c_404, c_4xx_other, c_5xx,
                    distinct_ips, p50_ms, p95_ms)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    target_date, r["route"], r["requests"], r["c_2xx"], r["c_402"], r["c_404"],
                    r["c_4xx_other"], r["c_5xx"], r["distinct_ips"], r["p50_ms"], r["p95_ms"],
                ),
            )
        for r in visitor_rows:
            conn.execute(
                """INSERT OR REPLACE INTO daily_visitors
                   (date, ip, user_agent, request_count, class, prober_name, is_new)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    target_date, r["ip"], r["user_agent"], r["request_count"],
                    r["class"], r["prober_name"], int(r["is_new"]),
                ),
            )

    print(
        f"ingested {target_date}: {len(route_rows)} routes, {len(visitor_rows)} distinct IPs, "
        f"{matched_lines} matching log lines"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
