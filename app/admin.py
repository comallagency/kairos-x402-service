import asyncio
import json
import logging
import secrets
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field, model_validator

from app import config, db
from app.upstream.openrouter import OpenRouterError, get_key_info

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
    return {
        "network": network,
        "revenue_usdc": {
            "24h": db.chain_revenue_since(network, 24),
            "7d": db.chain_revenue_since(network, 24 * 7),
            "30d": db.chain_revenue_since(network, 24 * 30),
        },
        "buyers": db.chain_buyer_stats(network),
        "last_payment_at": db.chain_last_payment_at(network),
        "chain": db.chain_summary(network),
        "calls_by_route": db.calls_by_route_since(24, network=network),
    }


def _activity_stats(events: list[dict]) -> dict:
    by_status: dict[str, int] = {}
    by_route: dict[str, int] = {}
    paid_usdc = 0.0
    for row in events:
        st = row.get("status") or "unknown"
        by_status[st] = by_status.get(st, 0) + 1
        rt = row.get("route") or "?"
        by_route[rt] = by_route.get(rt, 0) + 1
        if st == "paid" and row.get("amount_usdc") is not None:
            try:
                paid_usdc += float(row["amount_usdc"])
            except (TypeError, ValueError):
                pass
    return {
        "total": len(events),
        "by_status": by_status,
        "by_route": by_route,
        "paid_usdc": round(paid_usdc, 6),
    }


_last_chain_sync_at: float = 0.0
_CHAIN_SYNC_MIN_INTERVAL_S = 300.0  # live page polls every 3s — don't hammer RPC


async def collect_dashboard_data() -> dict:
    global _last_chain_sync_at
    now = time.monotonic()
    if now - _last_chain_sync_at >= _CHAIN_SYNC_MIN_INTERVAL_S:
        try:
            from chain_payments import sync as chain_sync

            await chain_sync()
            _last_chain_sync_at = time.monotonic()
        except Exception:
            # Keep serving last known chain rows; avoid traceback spam every poll.
            logger.warning(
                "live chain_payments sync failed, serving last known chain data",
                exc_info=True,
            )
            _last_chain_sync_at = time.monotonic()  # back off even on failure

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

    network = config.X402_NETWORK
    main = _network_block(network)
    generated_at = db.now_iso()

    from app.x402_setup import build_route_configs

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
                "price": payment.price,
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

    data = {
        "generated_at": generated_at,
        "updated_at": generated_at,
        "network": network,
        "revenue_usdc": main["revenue_usdc"],
        "buyers": main["buyers"],
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
        "events": events_payload,
        "stats_24h": _activity_stats(recent),
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


@router.get("/admin/data.json", include_in_schema=False)
async def admin_data(_: None = Depends(check_auth)):
    return await collect_dashboard_data()


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
        "journal": db.recent_journal(100),
        "index_visibility": db.latest_index_checks(20),
        "billing_without_delivery": billing_without_delivery,
    }


async def _openrouter_budget_status() -> dict:
    """Checked by usine/loop_run.sh before every cycle (drift-stop condition
    6: "le quota OpenRouter passe sous 10% de la journée"). This key has no
    OpenRouter-side `limit` configured (verified against /api/v1/key -
    limit/limit_remaining are null), so there is no native "% remaining" to
    read - pct_remaining here is against OUR OWN configured daily budget
    (config.OPENROUTER_DAILY_BUDGET_USD), not an OpenRouter-enforced cap."""
    try:
        key_info = (await get_key_info())["data"]
    except OpenRouterError as exc:
        return {"usage_daily": None, "daily_budget_usd": config.OPENROUTER_DAILY_BUDGET_USD, "pct_remaining": None, "error": str(exc)[:200]}
    usage_daily = key_info.get("usage_daily") or 0.0
    budget = config.OPENROUTER_DAILY_BUDGET_USD
    pct_remaining = max(0.0, 1.0 - (usage_daily / budget)) if budget > 0 else None
    return {"usage_daily": usage_daily, "daily_budget_usd": budget, "pct_remaining": pct_remaining, "error": None}


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
