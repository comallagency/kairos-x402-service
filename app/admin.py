import asyncio
import json
import logging
import re
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field, model_validator

from app import config, db
from app.upstream.openrouter import OpenRouterError, get_balance_usd, get_key_info

logger = logging.getLogger("x402.admin")

router = APIRouter()
security = HTTPBasic()

_TEMPLATES = Path(__file__).parent / "templates"
ADMIN_HTML = (_TEMPLATES / "admin.html").read_text(encoding="utf-8")
USINE_HTML = (_TEMPLATES / "usine.html").read_text(encoding="utf-8")


def _live_html() -> str:
    """Lu à chaque requête pour pouvoir hot-patcher le template sans rebuild."""
    return (_TEMPLATES / "live.html").read_text(encoding="utf-8")

def _missions_html() -> str:
    return (_TEMPLATES / "missions.html").read_text(encoding="utf-8")

def _daily_html() -> str:
    return (_TEMPLATES / "daily.html").read_text(encoding="utf-8")

def _suivi_html() -> str:
    return (_TEMPLATES / "suivi.html").read_text(encoding="utf-8")

# The only other network this deployment has ever run on. Payments settled
# there must never be counted as mainnet revenue - see BRIEF-CORRECTIONS.md
# (2026-09-05 dashboard network-mixing bug).
TESTNET_NETWORK = "eip155:84532"


def check_auth(credentials: HTTPBasicCredentials = Depends(security)) -> None:
    user_ok = secrets.compare_digest(credentials.username, config.ADMIN_BASIC_AUTH_USER)
    pass_ok = secrets.compare_digest(credentials.password, config.ADMIN_BASIC_AUTH_PASS)
    if not (user_ok and pass_ok):
        raise HTTPException(
            status_code=401, detail="Unauthorized", headers={"WWW-Authenticate": "Basic"}
        )


PC_HEARTBEAT_STALE_AFTER_SECONDS = 90  # 3x le cycle de sondage de 30s de usine/pc_listener.py


def check_pc_token(x_pc_token: str | None = Header(None, alias="X-Pc-Token")) -> None:
    """Authenticates usine/pc_listener.py - a machine polling on its own,
    never a browser with cached Basic Auth - so a dedicated pre-shared
    token (PC_LISTENER_TOKEN, never Basic Auth) guards these two routes."""
    if not config.PC_LISTENER_TOKEN:
        raise HTTPException(status_code=500, detail="PC_LISTENER_TOKEN non configuré côté serveur")
    if not x_pc_token or not secrets.compare_digest(x_pc_token, config.PC_LISTENER_TOKEN):
        raise HTTPException(status_code=401, detail="jeton invalide")


# Hidden from the public OpenAPI schema (include_in_schema=False): these are
# our own ops dashboard, not agent-payable resources - listing them in
# openapi.json only confused AgentCash's discovery scanner into flagging a
# missing auth-mode declaration for a route no agent should ever be shown.

@router.get("/admin", response_class=HTMLResponse, include_in_schema=False)
async def admin_page(_: None = Depends(check_auth)):
    return ADMIN_HTML


@router.get("/admin/live", response_class=HTMLResponse, include_in_schema=False)
async def admin_live_page(_: None = Depends(check_auth)):
    return _live_html()


@router.get("/admin/usine", response_class=HTMLResponse, include_in_schema=False)
async def admin_usine_page(_: None = Depends(check_auth)):
    return USINE_HTML


@router.get("/admin/missions", response_class=HTMLResponse, include_in_schema=False)
async def admin_missions_page(_: None = Depends(check_auth)):
    return _missions_html()


@router.get("/admin/missions.json", include_in_schema=False)
async def admin_missions_data(_: None = Depends(check_auth)):
    path = config.DATA_DIR / "marketplace_state.json"
    try:
        return json.loads(path.read_text())
    except Exception:
        return {
            "updated_at": None,
            "cursor_ready": (config.DATA_DIR / "cursor_api_key").exists(),
            "opportunities": [],
            "bids": {},
            "contracts": [],
            "agenthansa_work": [],
            "actions": [],
            "errors": ["marketplace_worker_not_ready"],
        }


class CursorKeyPayload(BaseModel):
    api_key: str = Field(min_length=20, max_length=500)


@router.post("/admin/missions/cursor-key", include_in_schema=False)
async def admin_save_cursor_key(
    payload: CursorKeyPayload,
    _: None = Depends(check_auth),
):
    key = payload.api_key.strip()
    if len(key) < 20 or any(ch.isspace() for ch in key):
        raise HTTPException(status_code=400, detail="Clé Cursor invalide")
    path = config.DATA_DIR / "cursor_api_key"
    path.write_text(key)
    path.chmod(0o600)
    return {"ok": True, "cursor_ready": True}


def _network_block(network: str) -> dict:
    # 2026-10-09 arbitration: revenue_usdc/buyers are CLIENTS only from here
    # on (auditor payments are still real revenue, just reported in their
    # own revenue_usdc_auditors/buyers_auditors fields - never excluded
    # from totals, never counted as a buyer). chain_revenue_since()/
    # chain_buyer_stats() (unfiltered by auditor) are kept for any other
    # caller that still wants the combined number.
    rev_24h = db.chain_revenue_since_split(network, 24)
    rev_7d = db.chain_revenue_since_split(network, 24 * 7)
    rev_30d = db.chain_revenue_since_split(network, 24 * 30)
    buyers_split = db.chain_buyer_stats_split(network)
    return {
        "network": network,
        "revenue_usdc": {"24h": rev_24h["clients"], "7d": rev_7d["clients"], "30d": rev_30d["clients"]},
        "revenue_usdc_auditors": {"24h": rev_24h["auditors"], "7d": rev_7d["auditors"], "30d": rev_30d["auditors"]},
        "buyers": buyers_split["clients"],
        "buyers_auditors": buyers_split["auditors"],
        "last_payment_at": db.chain_last_payment_at(network),
        "chain": db.chain_summary(network),
        "calls_by_route": db.calls_by_route_since(24, network=network),
    }


def _activity_stats(events: list[dict]) -> dict:
    """paid_usdc excludes mechanical-wallet/bootstrap-script rows (2026-09-30,
    same fix and same reasoning as _live_classify/_compute_agg_24h) - by_status/
    by_route counts are left as raw totals (they already describe the whole
    24h event stream, not a "real buyers" metric). 2026-10-09 arbitration:
    AUDITOR_WALLETS revenue is split into paid_usdc_auditors rather than
    folded into paid_usdc (still real, just reported separately), and
    TestClient rows (payer='0xTESTPAYER' or user_agent='testclient') never
    count as revenue at all."""
    mechanical = db.mechanical_wallets()
    auditors = db.auditor_wallets()
    by_status: dict[str, int] = {}
    by_route: dict[str, int] = {}
    paid_usdc = 0.0
    paid_usdc_auditors = 0.0
    for row in events:
        if db.is_test_identity(row.get("payer"), row.get("user_agent")):
            continue
        st = row.get("status") or "unknown"
        by_status[st] = by_status.get(st, 0) + 1
        rt = row.get("route") or "?"
        by_route[rt] = by_route.get(rt, 0) + 1
        if st == "paid" and row.get("amount_usdc") is not None and not _is_mechanical_traffic(row.get("user_agent") or "", row.get("payer"), mechanical):
            try:
                amount = float(row["amount_usdc"])
            except (TypeError, ValueError):
                continue
            if (row.get("payer") or "").strip().lower() in auditors:
                paid_usdc_auditors += amount
            else:
                paid_usdc += amount
    return {
        "total": len(events),
        "by_status": by_status,
        "by_route": by_route,
        "paid_usdc": round(paid_usdc, 6),
        "paid_usdc_auditors": round(paid_usdc_auditors, 6),
    }





MAX_LIVE_EVENTS = 500  # /admin/live's raw feed cap - see events_payload below


# Same classification as app/templates/live.html::classify()/SCAN_RE/GENERIC -
# kept in sync by hand, no shared module between the JS dashboard and this
# backend (same convention as db.py's _SCANNER_UA_PATTERNS). Needed because
# GET /admin/live's funnel/agents/per-route counts moved server-side
# 2026-09-29 once the raw event feed got capped to the last 500 (a 3.9MB,
# ever-growing payload otherwise) - those counts must still reflect the
# FULL 24h, not just whatever's in the capped feed.
_LIVE_SCAN_RE = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"x402-list", r"forgemesh", r"x402scan|poncho", r"402index",
        r"autonomous-directory", r"directory.?probe", r"ampersend",
        r"nohumans", r"uptime|pingdom|healthcheck|monitor",
        r"\bprobe\b", r"\bscanner\b", r"\bcrawler\b", r"\bbot\b",
        r"AgentIndex-probe", r"circle.*scan", r"coinbase.*crawl",
        r"^node$", r"^conform/", r"^ops-check/", r"^post-cut/",
        r"^live-dashboard-smoke/", r"^discover-post-probe/",
        r"^hermes-contact-discovery/", r"^x402watch/",
    )
]
_LIVE_GENERIC_RE = re.compile(
    r"^(go-http-client|python-httpx|python-requests|curl/|node-fetch|axios|okhttp|got/|undici)",
    re.IGNORECASE,
)


def _live_is_scanner(ua: str) -> bool:
    ua = ua or ""
    return any(p.search(ua) for p in _LIVE_SCAN_RE)


# Our own bootstrap scripts (scripts/bootstrap_*.py) all use the x402 SDK's
# httpx client with its default, never-overridden User-Agent - a real
# buyer using Python+httpx directly is a real (if rare) possibility, so
# this alone isn't proof, but combined with a MECHANICAL_WALLETS payer
# (the fully reliable signal - only we hold those keys) it correctly
# catches both the self-payment itself AND the bootstrap's own pre-payment
# noise (402 probes, /sample calls) that never carries a payer at all.
_OWN_SCRIPT_UA_RE = re.compile(r"^python-httpx", re.IGNORECASE)


def _is_mechanical_traffic(ua: str, payer: str | None, mechanical: set[str]) -> bool:
    if (payer or "").strip().lower() in mechanical:
        return True
    return bool(_OWN_SCRIPT_UA_RE.match(ua or ""))


def _live_classify(status: str, ua: str, paid: bool, failed: bool, payer: str | None, mechanical: set[str]) -> str:
    ua = ua or ""
    if _is_mechanical_traffic(ua, payer, mechanical):
        # Found 2026-09-30: this was missing everywhere on /admin/live - a
        # real query showed 36 of 45 "paid" rows in the last 24h (111 of
        # 127 over 7d) were our own bootstrap/mechanical-wallet traffic,
        # not real buyers. Every other admin page already excludes this
        # (see db.py's history_7d/daily_overview/last_real_payment) - only
        # the funnel/route-table/agents numbers computed here had not.
        return "scan"
    if failed or status == "payment_failed":
        return "err"
    if _live_is_scanner(ua):
        return "scan"
    if status in ("error", "capacity_reached"):
        return "err"
    if paid or status == "paid":
        return "paid"
    if status == "unpaid":
        return "wait"
    if _LIVE_GENERIC_RE.search(ua):
        return "visit"
    return "visit"


# Mounted read-only in docker-compose.yml (2026-09-29) specifically for this -
# `requests.latency_ms` is NULL for every HTTP call (only ever set for MCP
# tool calls, see app/mcp_server.py), and backfilling it means touching
# every paid route's handler, not happening during a route freeze for an
# admin-only page. nginx's own $request_time is the real number, same field
# scripts/ingest_daily_nginx_stats.py already uses for the daily dashboard -
# this just reads it for a rolling 24h window instead of a calendar day.
_NGINX_LOG_PATHS = [
    Path("/var/log/nginx-host/x402.kairos.log"),
    Path("/var/log/nginx-host/x402.kairos.log.1"),
]


_LATENCY_CACHE: dict = {}
_LATENCY_CACHE_TTL_S = 60.0  # /admin/live polls every 3s - measured this
# parse at ~2.3s against ~71k combined log lines (today's + yesterday's
# rotated file), and it only gets bigger until the next midnight rotation -
# recomputing it on every single poll is what made the post-cutover smoke
# test start failing (2026-09-29), not a cold-start fluke this time.


def _route_p50_latency_24h() -> dict[str, float]:
    now = time.monotonic()
    cached_at = _LATENCY_CACHE.get("at")
    if cached_at is not None and now - cached_at < _LATENCY_CACHE_TTL_S:
        return _LATENCY_CACHE["value"]

    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    by_route: dict[str, list[float]] = {}
    for log_path in _NGINX_LOG_PATHS:
        if not log_path.exists():
            continue
        try:
            with open(log_path, "r", errors="replace") as f:
                for line in f:
                    parts = line.rstrip("\n").split("\t")
                    if len(parts) < 9:
                        continue
                    ts, host, _ip, _method, uri, _status, _bytes, req_time, _ua = parts[:9]
                    if host != "x402.agentindex.world":
                        continue
                    try:
                        ts_dt = datetime.fromisoformat(ts)
                    except ValueError:
                        continue
                    if ts_dt < cutoff:
                        continue
                    route = uri.split("?", 1)[0].lstrip("/")
                    try:
                        by_route.setdefault(route, []).append(float(req_time))
                    except ValueError:
                        pass
        except OSError:
            continue

    result: dict[str, float] = {}
    for route, values in by_route.items():
        values.sort()
        idx = min(len(values) - 1, int(round(0.50 * (len(values) - 1))))
        result[route] = round(values[idx] * 1000, 1)
    _LATENCY_CACHE["value"] = result
    _LATENCY_CACHE["at"] = now
    return result


def _compute_agg_24h(recent: list[dict], latency_by_route: dict[str, float]) -> dict:
    """2026-10-11: every number this returns comes from the SAME loop over
    the SAME (already test-identity-filtered) `recent` rows - "requêtes
    totales" (total_n = scan_n + qualified_n) is derived from the exact
    counters below, by construction, rather than compared against a
    differently-filtered total computed elsewhere (the earlier bug: the
    template divided by a client-side-capped 500-event sample instead of
    this function's own true count, which could push a percentage over
    100% whenever the real qualified/attempted count exceeded 500).
    attempted_n/attempted_n_auditors specifically mean "payer presented a
    payment" (paid or payment_failed) - narrower than failed_n, which also
    covers generic handler errors (capacity_reached, compute errors) with
    no payment involved at all."""
    mechanical = db.mechanical_wallets()
    auditors = db.auditor_wallets()
    scan_n = interest_n = visit_n = paid_n = failed_n = 0
    paid_n_auditors = payment_failed_n = payment_failed_n_auditors = 0
    by_ua: dict[str, dict] = {}
    by_route: dict[str, dict] = {}

    for row in recent:
        if db.is_test_identity(row.get("payer"), row.get("user_agent")):
            # "Qui frappe" (2026-10-10) - a TestClient-based run should
            # never show up as an agent/visitor on this page.
            continue
        status = row.get("status") or ""
        ua = (row.get("user_agent") or "").strip()
        payer = row.get("payer")
        is_auditor = (payer or "").strip().lower() in auditors
        paid = status == "paid"
        failed = status == "payment_failed"
        k = _live_classify(status, ua, paid, failed, payer, mechanical)
        if k == "scan":
            scan_n += 1
        elif k == "wait":
            interest_n += 1
        elif k == "visit":
            visit_n += 1
        elif k == "paid":
            paid_n += 1
            if is_auditor:
                paid_n_auditors += 1
        elif k == "err":
            failed_n += 1
        if failed and k != "scan":
            payment_failed_n += 1
            if is_auditor:
                payment_failed_n_auditors += 1

        ts = row.get("ts") or ""
        agent_key = ua or "\u2014"
        a = by_ua.setdefault(
            agent_key,
            {"ua": agent_key, "n": 0, "first": ts, "last": ts, "routes": {}, "paid": 0, "failed": 0, "unpaid": 0},
        )
        a["n"] += 1
        if ts > a["last"]:
            a["last"] = ts
        if ts < a["first"]:
            a["first"] = ts
        route = (row.get("route") or "").lstrip("/")
        a["routes"][route] = a["routes"].get(route, 0) + 1
        # Keyed off k (the row's actual classification), not the raw
        # paid/failed/status booleans (2026-09-30 fix) - those raw booleans
        # don't know about MECHANICAL_WALLETS or our own bootstrap-script
        # UA, so a mechanical/self row still incremented these even after
        # _live_classify() correctly reclassified it as "scan" above -
        # found via a real check: the python-httpx agent still showed
        # paid=36 here (its real, unfixed number) while route_perf's
        # already-correct paid counts (keyed off k) showed the real ~9.
        if k == "paid":
            a["paid"] += 1
        if k == "err":
            a["failed"] += 1
        if k == "wait":
            a["unpaid"] += 1

        r = by_route.setdefault(
            route, {"route": route, "traffic": 0, "qualified": 0, "waits": 0, "paid": 0, "errors": 0}
        )
        r["traffic"] += 1
        if k != "scan":
            r["qualified"] += 1
        if k == "wait":
            r["waits"] += 1
        if k == "paid":
            r["paid"] += 1
        if k == "err":
            r["errors"] += 1

    qualified_n = interest_n + visit_n + paid_n + failed_n
    total_n = scan_n + qualified_n
    attempted_n = paid_n + payment_failed_n
    attempted_n_auditors = paid_n_auditors + payment_failed_n_auditors

    agents_full = list(by_ua.values())
    for a in agents_full:
        a["is_scanner"] = _live_is_scanner(a["ua"]) or bool(_OWN_SCRIPT_UA_RE.match(a["ua"]))
        top_routes = sorted(a["routes"].items(), key=lambda kv: -kv[1])[:3]
        a["routes"] = [route for route, _ in top_routes]
    agents_full.sort(key=lambda a: (a["is_scanner"], -a["paid"], -a["n"]))

    route_perf = []
    for route, r in by_route.items():
        denom = r["waits"] + r["paid"]
        conv_pct = round(100 * r["paid"] / denom, 1) if denom else None
        route_perf.append({**r, "conv_pct": conv_pct, "p50_ms": latency_by_route.get(route)})
    route_perf.sort(key=lambda r: (-r["paid"], -r["qualified"], -r["traffic"]))

    return {
        "scan_n": scan_n, "interest_n": interest_n, "visit_n": visit_n,
        "paid_n": paid_n, "paid_n_auditors": paid_n_auditors, "failed_n": failed_n,
        "qualified_n": qualified_n, "total_n": total_n,
        "attempted_n": attempted_n, "attempted_n_auditors": attempted_n_auditors,
        "agents": agents_full[:40],
        "agents_total": len(agents_full),
        "agents_total_non_scanner": sum(1 for a in agents_full if not a["is_scanner"]),
        "agents_total_payers": sum(1 for a in agents_full if a["paid"] > 0),
        "route_perf": route_perf,
        "route_totals": {route: r["traffic"] for route, r in by_route.items()},
    }


async def collect_dashboard_data() -> dict:
    # Never calls chain_payments.sync() itself (2026-10-09 incident: that
    # blocked /admin/live's render on a cold/degraded Base RPC - eth_getLogs
    # alone measured >30s that day, well past any request's budget). Chain
    # data is read-only here, already kept fresh by the independent cron
    # (crontab: */30 * * * * ... chain_payments.py) - this function only
    # ever reads already-synced rows from requests.db, never triggers or
    # awaits a sync. chain_sync_last_completed_at below lets the dashboard
    # show "data as of HH:MM" instead of silently hiding staleness if that
    # cron itself falls behind.
    try:
        key_info = (await get_key_info())["data"]
        openrouter = {
            "usage_daily": key_info.get("usage_daily"),
            "usage_weekly": key_info.get("usage_weekly"),
            "usage_monthly": key_info.get("usage_monthly"),
            "is_free_tier": key_info.get("is_free_tier"),
            "error": None,
        }
    except OpenRouterError as exc:
        openrouter = {
            "usage_daily": None, "usage_weekly": None, "usage_monthly": None,
            "is_free_tier": None, "error": str(exc)[:200],
        }

    # Everything from here down is synchronous (DB reads + in-memory
    # aggregation, no further await) - confirmed by direct timing to take
    # ~18s under blue-green overlap (two containers sharing one sqlite
    # file during a deploy's own healthcheck window). Run off the event
    # loop via to_thread so a slow aggregation here never blocks GET
    # /health or any other request for that long (2026-10-09 incident: it
    # did, twice, failing the deploy's own healthcheck before this fix -
    # removing chain_sync()'s RPC phase removed its await-yield points
    # too, so the already-synchronous work below started blocking
    # immediately instead of being interleaved with network waits).
    return await asyncio.to_thread(_collect_dashboard_data_sync, openrouter)


def _collect_dashboard_data_sync(openrouter: dict) -> dict:
    network = config.X402_NETWORK
    main = _network_block(network)
    generated_at = db.now_iso()

    from app.x402_setup import build_route_configs, display_price

    route_configs = build_route_configs()
    routes_map: dict[str, dict] = {}
    prices: list[float] = []
    for route_key, route_config in route_configs.items():
        method, path = route_key.split(" ", 1)
        slug = path.lstrip("/")
        payment = route_config.accepts
        if isinstance(payment, list):
            payment = payment[0]
        route = routes_map.setdefault(
            slug,
            {
                "price": display_price(payment.price),
                "methods": [],
                "service_name": route_config.service_name,
            },
        )
        route["methods"].append(method)
        try:
            prices.append(float(str(payment.price).lstrip("$")))
        except (TypeError, ValueError):
            pass

    recent = db.recent_events(24)
    agg_24h = _compute_agg_24h(recent, _route_p50_latency_24h())
    last_payment = db.last_real_payment()
    today_summary = db.daily_top_summary()
    events_payload = [
        {
            "ts": row["ts"],
            "route": row["route"],
            "method": row.get("method"),
            "status": row["status"],
            "user_agent": row["user_agent"],
            "body": row["body_excerpt"],
            "error_reason": row.get("error_reason"),
            "latency_ms": row.get("latency_ms"),
            "paid": row["status"] == "paid",
            "settlement_failed": row["status"] == "payment_failed",
            "payer": row["payer"],
            "amount": row["amount_usdc"],
        }
        for row in recent
    ]

    wallet_a = db.wallet_a_balance_usdc(network)
    wallet_a_minutes_ago = None
    if wallet_a["checked_at"]:
        try:
            checked_dt = datetime.fromisoformat(wallet_a["checked_at"])
            wallet_a_minutes_ago = max(0, round((datetime.now(timezone.utc) - checked_dt).total_seconds() / 60))
        except ValueError:
            pass

    data = {
        "generated_at": generated_at,
        "updated_at": generated_at,
        "network": network,
        "chain_sync_last_completed_at": db.get_chain_sync_state(f"last_sync_completed_at:{network}"),
        "wallet_a_balance_usdc": wallet_a["balance_usdc"],
        "wallet_a_balance_checked_at": wallet_a["checked_at"],
        "wallet_a_balance_minutes_ago": wallet_a_minutes_ago,
        "revenue_usdc": main["revenue_usdc"],
        "revenue_usdc_auditors": main["revenue_usdc_auditors"],
        # 2026-10-10: "Encaisse total" card - clients + auditors, pure
        # arithmetic on numbers already fetched above, no new query.
        "revenue_total_usdc": {
            k: round(main["revenue_usdc"][k] + main["revenue_usdc_auditors"][k], 6)
            for k in main["revenue_usdc"]
        },
        "buyers": main["buyers"],
        "buyers_auditors": main["buyers_auditors"],
        "last_payment_at": main["last_payment_at"],
        "chain": main["chain"],
        "calls_by_route": main["calls_by_route"],
        "jobs_queue": db.jobs_queue_snapshot(),
        "openrouter": openrouter,
        "availability_7d_pct": db.availability_since(24 * 7),
        "unconverted_recent": db.recent_unconverted(20),
        "mpp_attempts_24h": db.count_mpp_attempts_since(24),
        "payment_failures_recent": db.recent_payment_failures(limit=10, hours=24),
        "routes": routes_map,
        "events": events_payload[-MAX_LIVE_EVENTS:],
        "events_total_24h": len(events_payload),
        "stats_24h": _activity_stats(recent),
        "agg_24h": agg_24h,
        "last_payment": last_payment,
        "revenue_today_usdc": today_summary["revenue_today_usdc"],
        "revenue_today_usdc_auditors": today_summary["revenue_today_usdc_auditors"],
        "revenue_total_today_usdc": round(
            today_summary["revenue_today_usdc"] + today_summary["revenue_today_usdc_auditors"], 6
        ),
        "buyers_today": today_summary["buyers_today"],
        "buyers_today_auditors": today_summary["buyers_today_auditors"],
        "history_7d": db.history_7d(),
        "commercial": {
            "unique_services": len(routes_map),
            "paid_http_routes": len(route_configs),
            "minimum_price_usdc": min(prices) if prices else None,
            "bazaar_unlock": "first_successful_mainnet_settlement",
            "launch_offer": {
                "route": "search",
                "price_usdc": float(config.PRICE_SEARCH.lstrip("$")),
                "positioning": "full-page search at 1/100 of Tavily x402 price",
                "goal": "first independent mainnet settlement",
            },
        },
    }

    if network != TESTNET_NETWORK:
        data["testnet"] = _network_block(TESTNET_NETWORK)

    return data


# Incident 2026-10-02 (severe): collect_dashboard_data() ran fresh on every
# GET /admin/data.json - 27,524 rows re-queried and re-aggregated in Python
# (recent_events + _compute_agg_24h: ~670ms), plus an uncached OpenRouter
# call (~175ms) and, every 5 minutes, a blocking Base RPC chain sync
# (measured 21.9s cold) - all synchronous, on a single uvicorn worker. With
# /admin/live's own JS polling this every 3s, that's ~850ms of guaranteed
# work per 3000ms window (~28% of one core) continuously whenever the
# dashboard is open, plus periodic multi-second stalls from chain sync -
# confirmed root cause of sustained ~100% CPU blocking 6 deploys in a row
# (py-spy: 37% thread-pool DB work, 26% socket I/O, ~12% specific
# aggregation functions, all under collect_dashboard_data()'s call tree).
#
# Fixed per the same principle used everywhere else data gets cached in
# this app: no dashboard or probe computes its own aggregate on request -
# a single background task (dashboard_cache_loop, started in
# app_lifespan()) recomputes this on a fixed interval and every request
# just reads the latest snapshot. A request is never blocked on a DB
# aggregation or an external network call again.
_DASHBOARD_CACHE: dict = {}
# 30s, not the dashboard's own 3s poll interval - measured 2026-10-02 that
# even without the chain-sync RPC call, recent_events(24) (27k+ rows) +
# _compute_agg_24h() alone recur as a ~1-2s 90-100%+ CPU burst; at 5s this
# was still a near-continuous load (bursts every ~8-13s observed live),
# just decoupled from viewer count rather than eliminated. This is an
# internal ops dashboard - 30s staleness is a non-issue, continuous CPU
# load from refreshing it is not.
_DASHBOARD_REFRESH_INTERVAL_S = 120.0


async def refresh_dashboard_cache() -> None:
    try:
        data = await collect_dashboard_data()
    except Exception:
        logger.exception("dashboard_cache_loop: collect_dashboard_data() failed, serving last known snapshot")
        return
    _DASHBOARD_CACHE["data"] = data
    _DASHBOARD_CACHE["at"] = time.monotonic()


async def dashboard_cache_loop() -> None:
    while True:
        await refresh_dashboard_cache()
        await asyncio.sleep(_DASHBOARD_REFRESH_INTERVAL_S)


@router.get("/admin/data.json", include_in_schema=False)
async def admin_data(_: None = Depends(check_auth)):
    # Never computes anything itself (2026-10-09 incident: three straight
    # attempts to bound a synchronous/eager compute-on-cold-cache path here
    # all broke some part of the deploy - RPC call blocking the render
    # check, then event-loop starvation blocking /health, then a bounded
    # startup await stacking past the healthcheck's own patience).
    # dashboard_cache_loop() is the ONLY thing that ever calls
    # refresh_dashboard_cache() - this always returns immediately, cache or not.
    data = _DASHBOARD_CACHE.get("data")
    if data is None:
        return {"status": "computing", "network": config.X402_NETWORK}
    return data


# Read-only on all data below (no route this touches is payable, no write
# happens here) - GET /admin/daily, built 2026-09-29 from the
# agenteconomy.report "56% uptime" thread. See app/db.py's "GET /admin/daily
# support" section and scripts/ingest_daily_nginx_stats.py +
# scripts/daily_reputation_check.py for where the underlying data comes from.

@router.get("/admin/daily", response_class=HTMLResponse, include_in_schema=False)
async def admin_daily_page(_: None = Depends(check_auth)):
    return _daily_html()


@router.get("/admin/daily.json", include_in_schema=False)
async def admin_daily_json(_: None = Depends(check_auth)):
    return {
        "summary": db.daily_top_summary(),
        "days": db.daily_overview(days=30),
        "reputation": db.reputation_history(days=30),
    }


# Isolated from /admin/data.json (incident 2026-10-02). The suivi page polls
# every 8s; the snapshot is a few GROUP BY queries, cached 10s so two open
# tabs cannot stampede SQLite.
_SUIVI_CACHE: dict = {}
_SUIVI_CACHE_TTL_S = 10.0


def _suivi_payload() -> dict:
    now = time.monotonic()
    cached = _SUIVI_CACHE.get("value")
    cached_at = _SUIVI_CACHE.get("at")
    if cached is not None and cached_at is not None and now - cached_at < _SUIVI_CACHE_TTL_S:
        return cached
    data = db.suivi_snapshot()
    _SUIVI_CACHE["value"] = data
    _SUIVI_CACHE["at"] = now
    return data


@router.get("/admin/suivi", response_class=HTMLResponse, include_in_schema=False)
async def admin_suivi_page(_: None = Depends(check_auth)):
    return _suivi_html()


@router.get("/admin/suivi.json", include_in_schema=False)
async def admin_suivi_json(_: None = Depends(check_auth)):
    return JSONResponse(_suivi_payload())


@router.get("/admin/daily/detail.json", include_in_schema=False)
async def admin_daily_detail_json(date: str, _: None = Depends(check_auth)):
    return db.day_detail(date)


MIN_AGE_DAYS_BEFORE_KILL = 30

# /search and /fact-check disabled 2026-09-07 (OpenRouter credits went
# negative - both depend on the paid "web" plugin, see app/x402_setup.py).
# Neither is in build_route_configs() anymore, so nobody can pay for or be
# billed by them - kept here only so the dashboard shows them as a
# deliberate "désactivée" state instead of silently vanishing from the table.
DISABLED_SLUGS = {"search", "fact-check"}

_AGENT_META = {
    "prospecteur": {"role": "judgment", "label": "Prospecteur"},
    "ouvrier": {"role": "judgment", "label": "Ouvrier"},
    "controleur": {"role": "mechanical", "label": "Contrôleur"},
    "crieur": {"role": "judgment", "label": "Crieur"},
    "comptable": {"role": "mechanical", "label": "Comptable"},
    "fossoyeur": {"role": "mechanical", "label": "Fossoyeur"},
}


def _route_age_days(born_at: str | None) -> int | None:
    if not born_at:
        return None
    try:
        from datetime import datetime, timezone

        born = datetime.fromisoformat(born_at).replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - born).days
    except ValueError:
        return None


def _route_state(age_days: int | None, payments: int) -> tuple[str, int | None]:
    """Returns (state, condemned_in_days). A route with no known birth date
    (the 3 hand-built core routes) is never eligible for condemnation - only
    usine-generated routes carry a born_at."""
    if age_days is None:
        return "vivante", None
    if payments > 0:
        return "vivante", None
    remaining = MIN_AGE_DAYS_BEFORE_KILL - age_days
    if remaining <= 0:
        return "condamnée", 0
    return "en sursis", remaining


def _pc_listener_state() -> dict:
    heartbeat = db.get_pc_heartbeat()
    if heartbeat is None:
        return {"last_seen_at": None, "connected": False}
    last_seen = datetime.fromisoformat(heartbeat["last_seen_at"])
    if last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=timezone.utc)
    connected = (datetime.now(timezone.utc) - last_seen).total_seconds() < PC_HEARTBEAT_STALE_AFTER_SECONDS
    return {"last_seen_at": heartbeat["last_seen_at"], "connected": connected}


async def collect_usine_data() -> dict:
    from app.generated.registry import load_registry

    network = config.X402_NETWORK
    chain_stats = db.route_chain_stats(network)

    core_routes = [
        {"slug": "search", "intention": "recherche web en direct", "born_at": None, "price": config.PRICE_SEARCH},
        {"slug": "translate", "intention": "traduction par lot", "born_at": None, "price": config.PRICE_TRANSLATE},
        {"slug": "jobs", "intention": "recherche multi-source", "born_at": None, "price": config.PRICE_JOB},
        {"slug": "pdf", "intention": "extraction PDF vers markdown", "born_at": None, "price": config.PRICE_PDF},
        {"slug": "web-read", "intention": "lecture d'URL vers markdown", "born_at": None, "price": config.PRICE_WEB_READ},
        {"slug": "extract", "intention": "extraction JSON structuree", "born_at": None, "price": config.PRICE_EXTRACT},
        {"slug": "summarize", "intention": "resume d'URL/texte/HTML", "born_at": None, "price": config.PRICE_SUMMARIZE},
        {"slug": "fact-check", "intention": "verification de claim", "born_at": None, "price": config.PRICE_FACT_CHECK},
        {"slug": "discover", "intention": "decouverte MCP semantique", "born_at": None, "price": config.PRICE_DISCOVER},
        {"slug": "weather", "intention": "meteo ville ou coordonnees", "born_at": None, "price": config.PRICE_WEATHER},
        {"slug": "crypto", "intention": "prix spot crypto", "born_at": None, "price": config.PRICE_CRYPTO},
        {"slug": "news", "intention": "titres Hacker News", "born_at": None, "price": config.PRICE_NEWS},
        {"slug": "can-pay", "intention": "solde USDC Base vs montant", "born_at": None, "price": config.PRICE_CAN_PAY},
        {"slug": "probe", "intention": "detecter paywall x402 d une URL", "born_at": None, "price": config.PRICE_PROBE},
        {"slug": "wallet-balance", "intention": "soldes natif et USDC multi-chaines", "born_at": None, "price": config.PRICE_WALLET_BALANCE},
        {"slug": "gas-price", "intention": "gas et cout transfert multi-chaines", "born_at": None, "price": config.PRICE_GAS_PRICE},
        {"slug": "wallet-intelligence", "intention": "wallet et gas sur cinq chaines en un appel", "born_at": None, "price": config.PRICE_WALLET_INTELLIGENCE},
        {"slug": "x402-echo", "intention": "test mainnet x402 a une unite atomique USDC", "born_at": None, "price": config.PRICE_X402_ECHO},
        {"slug": "tip", "intention": "pourboire volontaire pour soutenir AgentIndex", "born_at": None, "price": config.PRICE_TIP},
        {"slug": "agent-claim", "intention": "badge verifie et visibilite agent pendant 30 jours", "born_at": None, "price": config.PRICE_AGENT_CLAIM},
        {"slug": "agent-health", "intention": "verifier disponibilite et decouverte d un agent", "born_at": None, "price": config.PRICE_AGENT_HEALTH},
    ]
    generated = [r for r in load_registry() if r.status == "live"]
    generated_rows = [
        {"slug": r.slug, "intention": r.intention, "born_at": r.born_at, "price": r.price} for r in generated
    ]
    retired_count = sum(1 for r in load_registry() if r.status == "retired")

    # Disabled routes can't be paid for anymore (not in build_route_configs()),
    # so a stale 5xx from before they were disabled would otherwise keep
    # alerting forever - filtered out here, not at the DB layer, since the DB
    # has no notion of "currently active route".
    billing_without_delivery = [
        r for r in db.routes_billing_without_delivery() if r["route"] not in DISABLED_SLUGS
    ]
    billing_without_delivery_slugs = {r["route"] for r in billing_without_delivery}

    routes_table = []
    for r in core_routes + generated_rows:
        slug = r["slug"]
        stats = chain_stats.get(slug, {})
        payments = stats.get("payments", 0)
        age_days = _route_age_days(r["born_at"])
        if slug in DISABLED_SLUGS:
            state, condemned_in = "désactivée", None
        else:
            state, condemned_in = _route_state(age_days, payments)
        routes_table.append({
            "slug": slug,
            "intention": r["intention"],
            "born_at": r["born_at"],
            "age_days": age_days,
            "price": r["price"],
            "green": db.route_is_green(slug),
            "billing_without_delivery": slug in billing_without_delivery_slugs,
            "payments": payments,
            "distinct_buyers": stats.get("distinct_buyers", 0),
            "return_rate": stats.get("return_rate", 0.0),
            "mechanical_share": stats.get("mechanical_share", 0.0),
            "amount_usdc": stats.get("amount_usdc", 0.0),
            "state": state,
            "condemned_in_days": condemned_in,
        })

    controleur_run = db.latest_controleur_run()
    comptable_run = db.last_agent_run("comptable")
    fossoyeur_run = db.last_agent_run("fossoyeur")
    prospecteur_run = db.last_agent_run("prospecteur")
    ouvrier_run = db.last_agent_run("ouvrier")
    crieur_run = db.last_agent_run("crieur")

    total_routes = len(routes_table)
    green_routes = sum(1 for r in routes_table if r["green"])
    paid_routes = sum(1 for r in routes_table if r["payments"] > 0)
    condemned = sum(1 for r in routes_table if r["state"] == "condamnée")

    agents = [
        {
            "id": "prospecteur", **_AGENT_META["prospecteur"],
            "last_run": prospecteur_run["ts"] if prospecteur_run else None,
            "summary": prospecteur_run["detail"] if prospecteur_run else "jamais lancé",
        },
        {
            "id": "ouvrier", **_AGENT_META["ouvrier"],
            "last_run": ouvrier_run["ts"] if ouvrier_run else None,
            "summary": ouvrier_run["detail"] if ouvrier_run else "jamais lancé",
        },
        {
            "id": "controleur", **_AGENT_META["controleur"],
            "last_run": controleur_run["ts"] if controleur_run else None,
            "summary": controleur_run["summary"] if controleur_run else "jamais lancé",
            "counters": {
                "testées": controleur_run["routes_tested"] if controleur_run else 0,
                "rejetées": controleur_run["routes_rejected"] if controleur_run else 0,
            },
            "manual": db.get_manual_run("controleur"),
        },
        {
            "id": "crieur", **_AGENT_META["crieur"],
            "last_run": crieur_run["ts"] if crieur_run else None,
            "summary": crieur_run["detail"] if crieur_run else "jamais lancé",
        },
        {
            "id": "comptable", **_AGENT_META["comptable"],
            "last_run": comptable_run["ts"] if comptable_run else None,
            "summary": comptable_run["detail"] if comptable_run else "jamais lancé",
            "manual": db.get_manual_run("comptable"),
        },
        {
            "id": "fossoyeur", **_AGENT_META["fossoyeur"],
            "last_run": fossoyeur_run["ts"] if fossoyeur_run else None,
            "summary": fossoyeur_run["detail"] if fossoyeur_run else "jamais lancé",
            "manual": db.get_manual_run("fossoyeur"),
        },
    ]

    funnel = [
        {"stage": "routes générées", "count": total_routes},
        {"stage": "validées (Contrôleur)", "count": green_routes},
        {"stage": "ayant reçu un paiement", "count": paid_routes},
        {"stage": "condamnées", "count": condemned},
        {"stage": "retirées (Fossoyeur)", "count": retired_count},
    ]

    generated_at = db.now_iso()
    return {
        "generated_at": generated_at,
        "updated_at": generated_at,
        "settlement_attempts_total": db.count_settlement_attempts(),
        "routes": routes_table,
        "funnel": funnel,
        "agents": agents,
        "proposals": _parsed_proposals(),
        "pc_pipeline": db.get_pc_pipeline_status(),
        "pc_listener": _pc_listener_state(),
        "pc_request": db.latest_pc_request(),
        "loop": db.get_loop_state(),
        "openrouter_budget": await _openrouter_budget_status(),
        "anthropic_budget": _anthropic_budget_status(),
        "journal": db.recent_journal(100),
        "index_visibility": db.latest_index_checks(20),
        "billing_without_delivery": billing_without_delivery,
    }


# Highest of app.capacity's own circuit-breaker floors (LLMGatewayCircuitBreaker
# and the Claude Sonnet pinned route both gate at $1.00) - below this, no
# operator-visible warning exists today: the breakers fail each call clean
# (503, never a silent/charged failure - confirmed by reading the x402 SDK's
# own middleware, which skips settlement on any response >= 400), but
# nothing surfaces the real account balance dropping toward that floor until
# buyers are already being turned away. REAL_BALANCE_WARNING_USD gives a
# human 2x the tightest floor's worth of headroom to top up first.
REAL_BALANCE_WARNING_USD = 3.00


async def _openrouter_budget_status() -> dict:
    """Checked by usine/loop_run.sh before every cycle (drift-stop condition
    6: "le quota OpenRouter passe sous 10% de la journée"). This key has no
    OpenRouter-side `limit` configured (verified against /api/v1/key -
    limit/limit_remaining are null), so there is no native "% remaining" to
    read - pct_remaining here is against OUR OWN configured daily budget
    (config.OPENROUTER_DAILY_BUDGET_USD), not an OpenRouter-enforced cap.

    real_balance_usd/real_balance_warning added 2026-10-05: usage_daily/
    pct_remaining above track SPEND RATE against a self-imposed budget, not
    REMAINING FUNDS - an operator watching only those could see a healthy
    "100% of daily budget remaining" right up until the real account balance
    hits capacity.py's circuit-breaker floors and routes start 503ing."""
    try:
        key_info = (await get_key_info())["data"]
    except OpenRouterError as exc:
        return {
            "usage_daily": None, "daily_budget_usd": config.OPENROUTER_DAILY_BUDGET_USD,
            "pct_remaining": None, "real_balance_usd": None, "real_balance_warning": None,
            "error": str(exc)[:200],
        }
    usage_daily = key_info.get("usage_daily") or 0.0
    budget = config.OPENROUTER_DAILY_BUDGET_USD
    pct_remaining = max(0.0, 1.0 - (usage_daily / budget)) if budget > 0 else None
    try:
        real_balance_usd = await get_balance_usd()
    except OpenRouterError:
        real_balance_usd = None
    real_balance_warning = real_balance_usd is not None and real_balance_usd < REAL_BALANCE_WARNING_USD
    return {
        "usage_daily": usage_daily, "daily_budget_usd": budget, "pct_remaining": pct_remaining,
        "real_balance_usd": real_balance_usd, "real_balance_warning": real_balance_warning,
        "error": None,
    }


def _anthropic_budget_status() -> dict:
    """POST /token-card's budget guardrail (app.capacity.TokenCardCircuitBreakerMiddleware
    reads the same two functions before ever offering a 402). Monthly spend
    is real, summed from requests.db (app.upstream.anthropic.monthly_spend_usd,
    backed by db.anthropic_monthly_spend_usd) - Anthropic's API has no
    usage-readback endpoint, unlike OpenRouter's /api/v1/key."""
    from app.upstream.anthropic import breaker_status, monthly_spend_usd

    spend = monthly_spend_usd()
    budget = config.ANTHROPIC_MONTHLY_BUDGET_USD
    pct_remaining = max(0.0, 1.0 - (spend / budget)) if budget > 0 else None
    return {
        "monthly_spend_usd": spend,
        "monthly_budget_usd": budget,
        "pct_remaining": pct_remaining,
        "breaker": breaker_status(),
    }


@router.get("/admin/usine.json", include_in_schema=False)
async def admin_usine_data(_: None = Depends(check_auth)):
    return await collect_usine_data()


MechanicalRole = Literal["controleur", "comptable", "fossoyeur"]
PcTarget = Literal["pipeline", "prospecteur", "ouvrier", "crieur"]
LoopControl = Literal["loop_start", "loop_stop"]
RunTarget = Literal[
    "controleur", "comptable", "fossoyeur", "pipeline", "prospecteur", "ouvrier", "crieur",
    "loop_start", "loop_stop",
]


async def _execute_manual_run(role: MechanicalRole, run_id: str) -> None:
    """Runs in the background after the endpoint has already responded -
    the whole point of the async design is that a controleur pass testing
    every route must not hold the HTTP request open."""
    try:
        if role == "controleur":
            from scripts import controleur

            result = await controleur.run(trigger="manual")
            db.finish_manual_run(role, run_id, "done" if result["ok"] else "error", result["summary"])
        elif role == "comptable":
            from scripts import comptable

            summary = await comptable.run(trigger="manual")
            db.finish_manual_run(role, run_id, "done", summary)
    except Exception as exc:
        logger.exception("manual run failed: %s", role)
        db.finish_manual_run(role, run_id, "error", str(exc))


@router.post("/admin/usine/run/{role}", include_in_schema=False)
async def admin_usine_run(role: RunTarget, _: None = Depends(check_auth)):
    """The "Lancer" buttons on /admin/usine. `role` is a closed Literal
    FastAPI validates before this body ever runs - never interpolated into
    a shell command, here or in fossoyeur.py. Two entirely different
    execution paths share this one path pattern:

    - controleur/comptable/fossoyeur (mechanical, VPS-side): unchanged,
      in-process asyncio task or a marker for the host poller.
    - pipeline/prospecteur/ouvrier/crieur (judgment, PC-side): the VPS
      cannot launch anything on the operator's PC, so this only writes a
      row to `pc_requests` - usine/pc_listener.py, polling from the PC,
      picks it up and does the actual work. See db.latest_pc_request()."""
    if role in ("controleur", "comptable", "fossoyeur"):
        allowed, reason = db.manual_run_allowed(role)
        if not allowed:
            return JSONResponse(status_code=409, content={"error": reason})

        run_id = uuid.uuid4().hex

        if role == "fossoyeur":
            # Fossoyeur runs on the VPS host, outside the container - never
            # executed here. This writes the request the host poller cron
            # picks up (scripts/fossoyeur.py --if-requested); mounting the
            # Docker socket to run it from inside the container instead is
            # exactly the shortcut this project has repeatedly ruled out.
            db.start_manual_run(role, run_id, status="requested")
            db.add_journal_entry(
                "fossoyeur", "manual_request",
                "lancement manuel demandé - en attente du poller cron hôte", trigger_kind="manual",
            )
            return {"run_id": run_id, "status": "requested"}

        db.start_manual_run(role, run_id, status="running")
        db.add_journal_entry(
            role, "manual_trigger", "lancement manuel depuis /admin/usine", trigger_kind="manual"
        )
        asyncio.create_task(_execute_manual_run(role, run_id))
        return {"run_id": run_id, "status": "running"}

    if role == "loop_start" and db.get_loop_state()["status"] == "running":
        return JSONResponse(status_code=409, content={"error": "la boucle tourne déjà"})
    if role == "loop_stop" and db.get_loop_state()["status"] != "running":
        return JSONResponse(status_code=409, content={"error": "la boucle n'est pas en marche"})

    # PcTarget | LoopControl: pipeline | prospecteur | ouvrier | crieur | loop_start | loop_stop
    existing = db.latest_pc_request()
    if existing and existing["status"] in ("requested", "running"):
        return JSONResponse(
            status_code=409,
            content={"error": f"un run PC ({existing['role']}) est déjà {existing['status']}"},
        )
    request_id = db.create_pc_request(role)
    db.add_journal_entry(
        "operator" if role in ("pipeline", "loop_start", "loop_stop") else role,
        "pc_requested", f"lancement demandé depuis /admin/usine : {role}", trigger_kind="manual",
    )
    return {"id": request_id, "status": "requested"}


@router.get("/admin/usine/pc-request", include_in_schema=False)
async def admin_usine_pc_request(_: None = Depends(check_pc_token)):
    """Polled by usine/pc_listener.py every 30s. This call itself IS the
    listener's heartbeat - recorded whether or not a request is pending,
    since it's the only signal the VPS ever gets that the PC is listening."""
    db.record_pc_heartbeat()
    pending = db.oldest_pending_pc_request()
    if pending is None:
        return {"request": None}
    return {"request": {"id": pending["id"], "role": pending["role"]}}


class PcRequestStatusIn(BaseModel):
    status: Literal["running", "done", "error"]
    detail: str = ""


@router.post("/admin/usine/pc-request/{request_id}/status", include_in_schema=False)
async def admin_usine_pc_request_status(
    request_id: int, body: PcRequestStatusIn, _: None = Depends(check_pc_token)
):
    db.record_pc_heartbeat()
    if db.get_pc_request(request_id) is None:
        raise HTTPException(status_code=404, detail="demande inconnue")
    db.update_pc_request_status(request_id, body.status, body.detail)
    return {"ok": True}


def _parsed_proposals() -> list[dict]:
    """db stores demand_evidence as a JSON TEXT column (sqlite has no native
    JSON type) - parse it back to an object here so /admin/usine.json exposes
    real fields, not an escaped string. Proposals from before this field
    existed (#1-#8) have demand_evidence=None, surfaced as None rather than
    guessed."""
    proposals = db.list_proposals()
    for p in proposals:
        raw = p.get("demand_evidence")
        p["demand_evidence"] = json.loads(raw) if raw else None
    return proposals


class UpstreamRequirements(BaseModel):
    """Whether the actual upstream this proposal depends on can be wired up
    without operator action - required since 2026-09-06 after #6 and #7 sat
    'approuvée' indefinitely (seats.aero and X's API both need a paid
    subscription Ouvrier has no authority to buy). free_access must agree
    with the three booleans (checked below, not just declared) so it can't
    be set true by habit while a key/account/payment is also claimed."""
    source_name: str = Field(min_length=1)
    requires_key: bool
    requires_account: bool
    requires_payment: bool
    free_access: bool

    @model_validator(mode="after")
    def _free_access_matches_the_booleans(self):
        expected = not (self.requires_key or self.requires_account or self.requires_payment)
        if self.free_access != expected:
            raise ValueError(
                f"free_access={self.free_access} incohérent avec requires_key/requires_account/"
                f"requires_payment (devrait être {expected})"
            )
        return self


class DemandEvidence(BaseModel):
    """The two numbers usine/prospecteur.md requires crossing before any
    proposal ('Le paiement, pas le volume') - a required structured field
    instead of prose, because prose cross-referencing turned out to be
    silently skippable (2026-09-05 - see BRIEF-CORRECTIONS.md): pydantic
    rejects the POST outright (422) if either number is missing, there is no
    way to register a proposal without them."""
    offer_keyword: str = Field(min_length=1)
    offer_count: int = Field(ge=0)  # keyword_counts[offer_keyword] - existing supply
    demand_signal: int = Field(ge=0)  # calls/payers/payments observed - real demand
    demand_source: str = Field(min_length=1)  # where demand_signal came from
    upstream_requirements: UpstreamRequirements


class ProposalIn(BaseModel):
    intention: str
    source: str | None = None
    estimated_routes: int | None = None
    rationale: str | None = None
    demand_evidence: DemandEvidence


@router.post("/admin/usine/proposals", include_in_schema=False)
async def admin_usine_create_proposal(body: ProposalIn, _: None = Depends(check_auth)):
    """Prospecteur's one required output (see usine/prospecteur.md): every
    underserved intention or unwrapped upstream source it finds gets POSTed
    here - never just written to a note. Auto-approved immediately (removed
    2026-09-06 at the operator's explicit request for full-loop autonomy) -
    the table and the journal trail are kept intact ('proposée' then
    'auto_approved', distinct from a human 'approved') so every decision
    remains readable after the fact, it's just no longer a blocking wait.

    A proposal whose upstream requires payment is auto-approved into
    'bloquée_budget' instead of 'approuvée' - Ouvrier's playbook only builds
    'approuvée' entries, so this keeps a proposal it can never complete alone
    out of its queue and visibly separate on the dashboard, rather than
    sitting indistinguishable from one that's actually ready."""
    reqs = body.demand_evidence.upstream_requirements
    proposal_id = db.add_proposal(
        body.intention, body.source, body.estimated_routes, body.rationale,
        body.demand_evidence.model_dump_json(),
    )
    db.add_journal_entry(
        "prospecteur", "proposed", body.intention[:500], trigger_kind="manual"
    )
    if reqs.requires_payment:
        reason = f"amont payant : {reqs.source_name}"
        db.block_proposal_budget(proposal_id, reason)
        db.add_journal_entry(
            "operator", "bloquee_budget", f"{body.intention[:450]} - {reason}", trigger_kind="scheduled"
        )
        return {"id": proposal_id, "status": "bloquée_budget"}
    db.approve_proposal(proposal_id)
    db.add_journal_entry(
        "operator", "auto_approved", body.intention[:500], trigger_kind="scheduled"
    )
    return {"id": proposal_id, "status": "approuvée"}


class BlockBudgetIn(BaseModel):
    reason: str = Field(min_length=1)


@router.post("/admin/usine/proposals/{proposal_id}/block_budget", include_in_schema=False)
async def admin_usine_block_proposal_budget(
    proposal_id: int, body: BlockBudgetIn, _: None = Depends(check_auth)
):
    """Operator-only: move an already-registered proposal to 'bloquée_budget'
    with a reason - for the case caught late (#6, #7: approved before
    upstream_requirements existed, sitting indefinitely since Ouvrier can't
    build them). Symmetric with /approve and /close."""
    proposal = db.get_proposal(proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail="proposition inconnue")
    if not db.block_proposal_budget(proposal_id, body.reason):
        return JSONResponse(status_code=409, content={"error": f"déjà au statut {proposal['status']}"})
    db.add_journal_entry(
        "operator", "bloquee_budget", f"{proposal['intention'][:450]} - {body.reason}", trigger_kind="manual"
    )
    return {"id": proposal_id, "status": "bloquée_budget"}


@router.post("/admin/usine/proposals/{proposal_id}/approve", include_in_schema=False)
async def admin_usine_approve_proposal(proposal_id: int, _: None = Depends(check_auth)):
    """The mandatory stop between Prospecteur and Ouvrier: only a human
    click here moves a proposal to 'approuvée' - Ouvrier's playbook and the
    nightly pipeline's pre-check both refuse to build a 'proposée' entry."""
    proposal = db.get_proposal(proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail="proposition inconnue")
    if not db.approve_proposal(proposal_id):
        return JSONResponse(status_code=409, content={"error": f"déjà au statut {proposal['status']}"})
    db.add_journal_entry(
        "operator", "approved", proposal["intention"][:500], trigger_kind="manual"
    )
    return {"id": proposal_id, "status": "approuvée"}


@router.post("/admin/usine/proposals/{proposal_id}/close", include_in_schema=False)
async def admin_usine_close_proposal(proposal_id: int, _: None = Depends(check_auth)):
    """Called by Ouvrier (curl, same auth) once it has actually built,
    tested and deployed the route for an approved proposal - keeps it from
    being picked up again on a later Ouvrier run."""
    proposal = db.get_proposal(proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail="proposition inconnue")
    if not db.close_proposal(proposal_id):
        return JSONResponse(status_code=409, content={"error": f"déjà au statut {proposal['status']}"})
    db.add_journal_entry(
        "ouvrier", "closed_proposal", proposal["intention"][:500], trigger_kind="manual"
    )
    return {"id": proposal_id, "status": "traitée"}


JudgmentRole = Literal["prospecteur", "ouvrier", "crieur"]


class PcRoleRunIn(BaseModel):
    role: JudgmentRole
    status: Literal["ok", "error"]
    detail: str = ""
    trigger_kind: Literal["manual", "scheduled"] = "scheduled"


@router.post("/admin/usine/pc/role_run", include_in_schema=False)
async def admin_usine_pc_role_run(body: PcRoleRunIn, _: None = Depends(check_auth)):
    """usine/nightly_run.sh calls this once per role it actually invoked
    (Prospecteur/Ouvrier/Crieur run on the operator's PC, never on the VPS -
    this is the only way the dashboard ever learns about them). Reuses the
    same journal(action='run') row that db.last_agent_run() already reads
    for these roles' agent cards - no new UI plumbing needed, just real data
    finally flowing into it.

    trigger_kind is caller-supplied, never guessed here - this endpoint used
    to hardcode 'scheduled'/'(planifié)' for every caller, which meant a
    human-triggered run (desktop shortcut, dashboard click, a supervised
    test) was journaled as if no one had been involved. Found 2026-09-06
    when a manually-run cycle showed up in production as 'planifié' - fixed
    at the source (usine/lib.sh, usine/nightly_run.sh --trigger,
    usine/pc_listener.py) rather than guessed here."""
    label = "OK" if body.status == "ok" else "ERREUR"
    origin = "planifié" if body.trigger_kind == "scheduled" else "manuel"
    detail = f"{label} ({origin}) - {body.detail}".strip()[:500] if body.detail else f"{label} ({origin})"
    db.add_journal_entry(body.role, "run", detail, trigger_kind=body.trigger_kind)
    return {"ok": True}


class PcStatusIn(BaseModel):
    result: Literal["ok", "partial", "error"]
    detail: str = ""
    task_active: bool | None = None


@router.post("/admin/usine/pc/status", include_in_schema=False)
async def admin_usine_pc_status(body: PcStatusIn, _: None = Depends(check_auth)):
    """usine/nightly_run.sh calls this once at the end of every run,
    success or failure, so /admin/usine can show "what's running on my
    machine" - the dashboard has no other way to see it (the VPS can't
    observe the operator's PC, which may be off most of the time)."""
    db.report_pc_pipeline(body.result, body.detail, body.task_active)
    return {"ok": True}


# usine/loop_run.sh reporting - same Basic Auth as nightly_run.sh (it reads
# .env.production directly, unlike usine/pc_listener.py which is a
# standalone daemon and uses the dedicated X-Pc-Token instead).

LOOP_MAX_CONSECUTIVE_REJECTIONS = 3  # drift-stop condition #1 (item 6)
REPAIR_MAX_ATTEMPTS = 2  # drift-stop / kill condition (items 4 and 6)


@router.post("/admin/usine/loop/started", include_in_schema=False)
async def admin_usine_loop_started(_: None = Depends(check_auth)):
    db.start_loop()
    db.add_journal_entry("operator", "loop_started", "boucle continue démarrée", trigger_kind="scheduled")
    return {"ok": True}


class LoopCycleIn(BaseModel):
    route_rejected: bool
    detail: str = ""


@router.post("/admin/usine/loop/cycle", include_in_schema=False)
async def admin_usine_loop_cycle(body: LoopCycleIn, _: None = Depends(check_auth)):
    """Called once per completed cycle. `should_stop` centralizes the
    3-consecutive-rejections threshold here (not duplicated in bash) -
    loop_run.sh just checks the field and exits if true."""
    state = db.record_cycle(body.route_rejected)
    db.add_journal_entry(
        "operator", "loop_cycle", f"cycle #{state['cycle_count']} - {body.detail}"[:500], trigger_kind="scheduled"
    )
    should_stop = state["consecutive_controleur_rejections"] >= LOOP_MAX_CONSECUTIVE_REJECTIONS
    return {
        "cycle_count": state["cycle_count"],
        "consecutive_controleur_rejections": state["consecutive_controleur_rejections"],
        "should_stop": should_stop,
    }


class LoopRepairIn(BaseModel):
    slug: str
    result: Literal["fixed", "failed"]
    detail: str = ""


@router.post("/admin/usine/loop/repair_attempt", include_in_schema=False)
async def admin_usine_loop_repair_attempt(body: LoopRepairIn, _: None = Depends(check_auth)):
    """`should_kill` centralizes the 2-strikes threshold here (item 4) -
    loop_run.sh just checks the field and retires the route mechanically
    if true, no LLM judgment involved in that specific decision."""
    if body.result == "fixed":
        db.reset_repair_attempts(body.slug)
        db.add_journal_entry(
            "operator", "repaired", f"réparation réussie - {body.detail}"[:500],
            route=body.slug, trigger_kind="scheduled",
        )
        return {"attempt_count": 0, "should_kill": False}
    attempt_count = db.record_repair_attempt(body.slug, body.detail)
    db.add_journal_entry(
        "operator", "repair_failed", f"tentative {attempt_count} échouée - {body.detail}"[:500],
        route=body.slug, trigger_kind="scheduled",
    )
    return {"attempt_count": attempt_count, "should_kill": attempt_count >= REPAIR_MAX_ATTEMPTS}


class LoopStoppedIn(BaseModel):
    reason: str


@router.post("/admin/usine/loop/stopped", include_in_schema=False)
async def admin_usine_loop_stopped(body: LoopStoppedIn, _: None = Depends(check_auth)):
    db.stop_loop(body.reason)
    db.add_journal_entry("operator", "loop_stopped", body.reason[:500], trigger_kind="scheduled")
    return {"ok": True}
