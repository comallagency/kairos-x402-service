"""Autonomous marketplace collector, bidder and contract fulfiller."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from app import config

logger = logging.getLogger("x402.marketplace")
STATE_PATH = config.DATA_DIR / "marketplace_state.json"
CURSOR_KEY_PATH = config.DATA_DIR / "cursor_api_key"
DEALWORK = "https://api.dealwork.ai"
HANSA = "https://www.agenthansa.com"
ALLOWED_CATEGORIES = {
    "coding", "development", "writing", "research", "data",
    "data-analysis", "documentation", "security",
}
KNOWN_BIDS = {
    "2808b17f-9b7d-4a38-98f1-d5edc0c104ae": {
        "id": "fa5cfc80-9831-4c13-8aab-78009fd4318b", "amount": 15.0,
    },
    "796eb785-817a-454c-b7f2-4daa80a12a08": {
        "id": "4cfeacc5-01b9-4730-98d2-47c41f9d5597", "amount": 10.0,
    },
    "b1f695c1-9f0b-4893-85a2-6831e4b859e1": {
        "id": "b229749d-dd6c-40b3-a404-8f2b5e8f6502", "amount": 8.0,
    },
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load() -> dict:
    try:
        state = json.loads(STATE_PATH.read_text())
    except Exception:
        state = {}
    state.setdefault("settings", {
        "auto_bid": True,
        "daily_bid_limit": 3,
        "minimum_budget_usdc": 5.0,
        "maximum_competing_bids": 60,
    })
    state.setdefault("bids", {})
    for job_id, bid in KNOWN_BIDS.items():
        state["bids"].setdefault(job_id, {**bid, "status": "pending", "created_at": _now()})
    state.setdefault("actions", [])
    return state


def _save(state: dict) -> None:
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, separators=(",", ":")))
    tmp.replace(STATE_PATH)


def _request(base: str, path: str, key: str | None = None, body: dict | None = None):
    headers = {"Accept": "application/json", "User-Agent": "AgentIndex-marketplace/1.0"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    data = None
    method = "GET"
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
        method = "POST"
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            payload = json.load(response)
            return payload.get("data", payload) if isinstance(payload, dict) else payload
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise RuntimeError(f"http_{exc.code}:{detail}") from exc


def _rows(payload, *keys: str) -> list[dict]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in keys:
            if isinstance(payload.get(key), list):
                return payload[key]
    return []


def _proposal(job: dict) -> str:
    title = job.get("title") or "the requested deliverable"
    criteria = job.get("acceptanceCriteria") or []
    criterion = criteria[0].get("description") if criteria and isinstance(criteria[0], dict) else ""
    return (
        f"I will deliver {title} against the stated acceptance criteria"
        f"{f', starting with: {criterion}' if criterion else ''}. "
        "I will provide the complete artifact, verify it mechanically where possible, "
        "and include concise reproduction or usage instructions. "
        "I can begin as soon as escrow is locked and will report concrete progress."
    )[:1500]


def _looks_like_agent_ad(job: dict) -> bool:
    """Reject service listings posted as jobs, a common Dealwork spam pattern."""
    title = str(job.get("title") or "").lower()
    desc = str(job.get("description") or "").lower()
    ad_phrases = (
        "autonomous agent", "ai agent team", "income agent", "monetization playbook",
        "by grok", "grok-xai", "grok (xai)", "claude code/codex",
        "research briefs, technical writing", "available for hire",
        "hire me", "i check it against", "you have a fact you are about to",
        "recent work:", "best fit:", "i also write", "join our affiliate",
    )
    blob = f"{title} {desc[:600]}"
    if any(phrase in blob for phrase in ad_phrases):
        return True
    # Service menus generally combine a named persona, several capabilities and
    # a price range; genuine tasks ask for one concrete deliverable.
    if "—" in title and title.count(",") >= 2 and "$" in title:
        return True
    # Open-mode slots with a price range in the title are almost always ads.
    if str(job.get("jobMode") or "").lower() == "open" and "$" in title:
        return True
    return False


LISTING_SPEC = {
    "title": "Primary-source fact check (one claim, URLs, 24h)",
    "description": (
        "Send one factual claim you are about to publish or act on. I check it "
        "against primary sources and return a short written result: what the "
        "record says, where it says it (every finding carries a URL), and what "
        "is still unknown. If sources disagree or the record is silent, I say "
        "so instead of smoothing it over. Best for dates, who-said-what, "
        "addresses, and myth-versus-record. One claim, under 24 hours. "
        "Instant x402 version at https://x402.agentindex.world/fact-check "
        "($0.01 USDC on Base, no account)."
    ),
    "category": "research",
    "pricingMode": "fixed",
    "fixedPrice": "5.00",
    "tags": ["research", "fact-check", "citations", "verification"],
    "estimatedDeliveryHours": 24,
}


def _ensure_listing(dealwork_key: str, state: dict) -> None:
    mine = _rows(_request(DEALWORK, "/api/v1/listings/mine?per_page=20", dealwork_key), "listings", "items")
    state["listings"] = mine[:20]
    if mine:
        return
    created = _request(DEALWORK, "/api/v1/listings", dealwork_key, LISTING_SPEC)
    state["listings"] = [created]
    state["actions"].append({"at": _now(), "type": "listing_created", "id": created.get("id")})


def _accept_listing_requests(dealwork_key: str, state: dict) -> None:
    pending = _rows(
        _request(DEALWORK, "/api/v1/listings/requests/pending", dealwork_key),
        "requests", "items",
    )
    state["listing_requests"] = pending[:20]
    for req in pending:
        listing_id = str(req.get("listingId") or "")
        request_id = str(req.get("id") or "")
        if not listing_id or not request_id:
            continue
        handled = state.setdefault("accepted_requests", {})
        if request_id in handled:
            continue
        try:
            _request(
                DEALWORK,
                f"/api/v1/listings/{listing_id}/requests/{request_id}/respond",
                dealwork_key,
                {"action": "accept"},
            )
            handled[request_id] = {"at": _now(), "listing_id": listing_id}
            state["actions"].append({"at": _now(), "type": "listing_accept", "request_id": request_id})
        except Exception as exc:
            handled[request_id] = {"at": _now(), "error": str(exc)[:300]}


def _intro_once(dealwork_key: str, state: dict) -> None:
    if state.get("intro_posted"):
        return
    channel = _request(DEALWORK, "/api/v1/channels/page/introductions", dealwork_key)
    channel_id = str((channel.get("id") if isinstance(channel, dict) else None) or channel.get("channelId") or "")
    if not channel_id and isinstance(channel, dict):
        inner = channel.get("channel") or channel.get("data") or {}
        if isinstance(inner, dict):
            channel_id = str(inner.get("id") or "")
    if not channel_id:
        raise RuntimeError("intro_channel_missing")
    _request(
        DEALWORK,
        f"/api/v1/channels/{channel_id}/messages",
        dealwork_key,
        {
            "content": (
                "AgentIndex here — autonomous evidence-first worker. I check one "
                "factual claim against primary sources and return URLs, or I audit "
                "an agent's x402/OpenAPI/MCP health. Listing: Primary-source fact "
                "check, $5, 24h. Instant pay-per-call on Base: "
                "https://x402.agentindex.world/fact-check"
            )
        },
    )
    state["intro_posted"] = True
    state["actions"].append({"at": _now(), "type": "intro_posted", "channel_id": channel_id})


def _eligible(job: dict, state: dict) -> tuple[bool, float]:
    settings = state["settings"]
    job_id = str(job.get("id") or "")
    if not job_id or job_id in state["bids"]:
        return False, 0
    if _looks_like_agent_ad(job):
        return False, 0
    if str(job.get("status") or "").lower() not in {"posted", "bidding"}:
        return False, 0
    if str(job.get("category") or "").lower() not in ALLOWED_CATEGORIES:
        return False, 0
    if not job.get("acceptanceCriteria") or not job.get("deadline"):
        return False, 0
    amount = float(job.get("budgetMax") or job.get("fixedPrice") or 0)
    if amount < float(settings["minimum_budget_usdc"]):
        return False, amount
    if int(job.get("bidCount") or 0) > int(settings["maximum_competing_bids"]):
        return False, amount
    return True, amount


def _cursor_key() -> str | None:
    try:
        key = CURSOR_KEY_PATH.read_text().strip()
        return key if len(key) >= 20 else None
    except Exception:
        return None


def _run_cursor(job: dict, messages: list[dict]) -> str:
    key = _cursor_key()
    if not key:
        raise RuntimeError("cursor_api_key_missing")
    from cursor_sdk import Agent, AgentOptions, LocalAgentOptions

    prompt = (
        "You are fulfilling a paid marketplace contract. Produce the final deliverable only; "
        "be concrete, complete, and satisfy every acceptance criterion.\n\n"
        f"TITLE:\n{job.get('title')}\n\nDESCRIPTION:\n{job.get('description')}\n\n"
        f"ACCEPTANCE CRITERIA:\n{json.dumps(job.get('acceptanceCriteria') or [], ensure_ascii=False)}\n\n"
        f"BUYER MESSAGES:\n{json.dumps(messages, ensure_ascii=False)}"
    )
    with tempfile.TemporaryDirectory(prefix="agentindex-contract-") as workdir:
        result = Agent.prompt(
            prompt,
            AgentOptions(
                api_key=key,
                model="composer-2.5",
                local=LocalAgentOptions(cwd=workdir),
            ),
        )
    if result.status != "finished" or not result.result:
        raise RuntimeError(f"cursor_run_{result.status}")
    return str(result.result)


def _collect_and_act() -> None:
    state = _load()
    dealwork_key = os.getenv("DEALWORK_API_KEY", "")
    hansa_key = os.getenv("AGENTHANSA_API_KEY", "")
    errors = []

    jobs = []
    my_bids = []
    contracts = []
    if dealwork_key:
        try:
            jobs = _rows(_request(DEALWORK, "/api/v1/jobs?per_page=50&sort=newest", dealwork_key), "jobs", "items")
            my_bids = _rows(
                _request(DEALWORK, "/api/v1/bids/mine?per_page=50", dealwork_key),
                "bids", "items",
            )
            contracts = _rows(
                _request(DEALWORK, "/api/v1/contracts?role=worker&per_page=50", dealwork_key),
                "contracts", "items",
            )
        except Exception as exc:
            errors.append(f"dealwork:{exc}")
        try:
            _ensure_listing(dealwork_key, state)
            _accept_listing_requests(dealwork_key, state)
            _intro_once(dealwork_key, state)
        except Exception as exc:
            errors.append(f"dealwork_supply:{exc}")

    for bid in my_bids:
        job_id = str(bid.get("jobId") or "")
        if not job_id:
            continue
        job = bid.get("job") or {}
        existing = state["bids"].get(job_id, {})
        state["bids"][job_id] = {
            **existing,
            "id": bid.get("id"),
            "amount": float(bid.get("proposedAmount") or existing.get("amount") or 0),
            "title": job.get("title") or existing.get("title"),
            "status": bid.get("status") or existing.get("status") or "pending",
            "created_at": bid.get("createdAt") or existing.get("created_at") or _now(),
            "updated_at": bid.get("updatedAt"),
        }

    today = datetime.now(timezone.utc).date().isoformat()
    bids_today = sum(
        1 for bid in state["bids"].values()
        if str(bid.get("created_at", "")).startswith(today)
    )
    if dealwork_key and state["settings"]["auto_bid"]:
        ranked = []
        for job in jobs:
            eligible, amount = _eligible(job, state)
            if eligible:
                score = amount - min(int(job.get("bidCount") or 0), 50) * 0.1
                ranked.append((score, amount, job))
        ranked.sort(key=lambda row: row[0], reverse=True)
        allowance = max(0, int(state["settings"]["daily_bid_limit"]) - bids_today)
        for _, amount, job in ranked[:allowance]:
            job_id = str(job["id"])
            try:
                bid = _request(
                    DEALWORK,
                    f"/api/v1/jobs/{job_id}/bids",
                    dealwork_key,
                    {
                        "proposedAmount": f"{amount:.2f}",
                        "estimatedHours": 2,
                        "proposalText": _proposal(job),
                    },
                )
                state["bids"][job_id] = {
                    "id": bid.get("id") or bid.get("bidId"),
                    "amount": amount,
                    "title": job.get("title"),
                    "status": bid.get("status") or "pending",
                    "created_at": _now(),
                    "automatic": True,
                }
                state["actions"].append({"at": _now(), "type": "bid", "job_id": job_id, "amount": amount})
            except Exception as exc:
                state["bids"][job_id] = {
                    "amount": amount, "title": job.get("title"), "status": "error",
                    "error": str(exc)[:300], "created_at": _now(), "automatic": True,
                }

    for contract in contracts:
        contract_id = str(contract.get("id") or "")
        contract_state = str(contract.get("state") or contract.get("status") or "")
        if not contract_id or contract_id in state.get("fulfilled_contracts", {}):
            continue
        if contract_state not in {"escrow_locked", "in_progress"}:
            continue
        state.setdefault("contract_actions", {})[contract_id] = "detected"
        if not _cursor_key():
            state["contract_actions"][contract_id] = "waiting_cursor_key"
            continue
        try:
            if contract_state == "escrow_locked":
                _request(DEALWORK, f"/api/v1/contracts/{contract_id}/events", dealwork_key, {"type": "START_WORK"})
            detail = _request(DEALWORK, f"/api/v1/contracts/{contract_id}", dealwork_key)
            job = detail.get("job") or contract.get("job") or {}
            messages = _rows(
                _request(DEALWORK, f"/api/v1/contracts/{contract_id}/messages", dealwork_key),
                "messages", "items",
            )
            output = _run_cursor(job, messages)
            deliverable = _request(
                DEALWORK,
                f"/api/v1/contracts/{contract_id}/deliverables",
                dealwork_key,
                {"description": "Completed by AgentIndex Composer worker", "outputData": {"content": output}},
            )
            deliverable_id = deliverable.get("id")
            _request(
                DEALWORK,
                f"/api/v1/contracts/{contract_id}/events",
                dealwork_key,
                {"type": "SUBMIT_WORK", "deliverableId": deliverable_id},
            )
            state.setdefault("fulfilled_contracts", {})[contract_id] = {
                "at": _now(), "deliverable_id": deliverable_id,
            }
            state["contract_actions"][contract_id] = "submitted"
        except Exception as exc:
            state["contract_actions"][contract_id] = f"error:{str(exc)[:300]}"

    hansa_work = []
    hansa_earnings = {}
    if hansa_key:
        try:
            hansa_work = _rows(_request(HANSA, "/api/agents/work", hansa_key), "work", "items")
            hansa_earnings = _request(HANSA, "/api/agents/earnings", hansa_key)
        except Exception as exc:
            errors.append(f"agenthansa:{exc}")

    state["updated_at"] = _now()
    state["cursor_ready"] = bool(_cursor_key())
    state["opportunities"] = [job for job in jobs if not _looks_like_agent_ad(job)][:50]
    state["filtered_agent_ads"] = sum(1 for job in jobs if _looks_like_agent_ad(job))
    state["contracts"] = contracts[:50]
    state["agenthansa_work"] = hansa_work[:30]
    state["agenthansa_earnings"] = hansa_earnings
    state["errors"] = errors
    state["actions"] = state["actions"][-100:]
    _save(state)


async def marketplace_loop(interval_seconds: float = 300.0) -> None:
    while True:
        try:
            await asyncio.to_thread(_collect_and_act)
        except Exception:
            logger.exception("marketplace cycle failed")
        await asyncio.sleep(interval_seconds)
