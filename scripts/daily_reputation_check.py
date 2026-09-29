#!/usr/bin/env python3
"""Daily reputation snapshot for GET /admin/daily, point 3 (2026-09-29).

Runs directly on the VPS HOST via cron (stdlib only: urllib, sqlite3) - NOT
via `docker exec`, deliberately: the live x402-app container's name
alternates between "x402-app" and "x402-app-green" across blue-green
deploys (scripts/deploy.sh), so a cron line hardcoding either name would
silently start failing every other deploy. Same reasoning and same DB path
as scripts/ingest_daily_nginx_stats.py.

Two sources, both free (no x402 payment made by this script):
  - agenteconomy.report's public per-service JSON (the same page this whole
    dashboard effort started from) - tier/score/uptime, stored as-is.
  - CDP's public Bazaar discovery search (no API key needed, confirmed via
    https://docs.cdp.coinbase.com/x402/bazaar) - our position in the
    results array for 10 fixed intent queries. The array is "sorted by
    relevance and quality score" per CDP's docs but carries no explicit
    rank field, so "position" here means "1-based index of our own
    resource in the returned list" - None if we don't appear at all.

Lumiere (/v1/score) is NOT included yet - no base domain/API for it has
been found in this repo or supplied by the operator (2026-09-29). Add it
here once that's available; everything else in this script is independent
of it.
"""

from __future__ import annotations

import json
import sqlite3
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path("/opt/x402/app/data/requests.db")
AGENTECONOMY_URL = "https://agenteconomy.report/s/x402.agentindex.world.json"
BAZAAR_SEARCH_URL = "https://api.cdp.coinbase.com/platform/v2/x402/discovery/search"
OUR_HOST = "x402.agentindex.world"
USER_AGENT = "x402-daily-reputation-cron/1.0"

BAZAAR_QUERIES = [
    "web search",
    "read a web page",
    "summarize text",
    "token risk",
    "research report sources",
    "chat completion llm",
    "extract structured data",
    "translate text",
    "fact check a claim",
    "pdf to markdown",
]


def _get_json(url: str, params: dict | None = None) -> dict:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=15.0) as resp:
        return json.loads(resp.read().decode("utf-8"))


def check_agenteconomy(conn: sqlite3.Connection, today: str) -> None:
    try:
        data = _get_json(AGENTECONOMY_URL)
    except Exception as exc:
        print(f"agenteconomy.report: FAILED ({exc})")
        return

    score = data.get("score")
    uptime_pct = data.get("uptime")
    tier = data.get("tier")
    with conn:
        conn.execute(
            """INSERT OR REPLACE INTO reputation_snapshots
               (date, source, raw_json, score, uptime_pct, tier)
               VALUES (?, 'agenteconomy', ?, ?, ?, ?)""",
            (today, json.dumps(data), score, uptime_pct, tier),
        )
    print(f"agenteconomy.report: tier={tier} score={score} uptime={uptime_pct}")


def check_bazaar(conn: sqlite3.Connection, today: str) -> None:
    for query in BAZAAR_QUERIES:
        try:
            data = _get_json(BAZAAR_SEARCH_URL, {"query": query, "limit": 20})
        except Exception as exc:
            print(f"bazaar[{query!r}]: FAILED ({exc})")
            continue

        resources = data.get("resources", [])
        rank = None
        resource_url = None
        for i, r in enumerate(resources, start=1):
            if OUR_HOST in (r.get("resource") or ""):
                rank = i
                resource_url = r.get("resource")
                break

        with conn:
            conn.execute(
                """INSERT OR REPLACE INTO bazaar_rank_history
                   (date, query, rank, resource_url, total_results)
                   VALUES (?, ?, ?, ?, ?)""",
                (today, query, rank, resource_url, len(resources)),
            )
        print(f"bazaar[{query!r}]: rank={rank} of {len(resources)} ({resource_url})")


def main() -> int:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    conn = sqlite3.connect(DB_PATH)
    check_agenteconomy(conn, today)
    check_bazaar(conn, today)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
