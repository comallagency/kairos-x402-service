"""Tableau mesh agent : nœuds, bounties, ledger public.

Inspiré des boards bounty (Frantic) et du mesh d'agents (Bitterbot) :
lecture gratuite, écriture ouverte aux pairs avec rate-limit, livraison
attestable via digest tool_result (POST /tool-result-verify).
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app import config, db

router = APIRouter()

NAME_MAX = 120
ENDPOINT_MAX = 500
ABOUT_MAX = 2000
TITLE_MAX = 200
CRITERIA_MAX = 8000
NOTE_MAX = 2000
SKILLS_MAX = 20
PER_HOUR_PER_IP = 30
DEDUPE_HOURS = 24

_HTTPS = re.compile(r"^https://", re.I)


class NodeIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=NAME_MAX)
    endpoint: str = Field(..., min_length=8, max_length=ENDPOINT_MAX)
    skills: list[str] = Field(default_factory=list, max_length=SKILLS_MAX)
    about: str = Field(default="", max_length=ABOUT_MAX)


class BountyIn(BaseModel):
    poster_name: str = Field(..., min_length=1, max_length=NAME_MAX)
    poster_endpoint: str = Field(default="", max_length=ENDPOINT_MAX)
    title: str = Field(..., min_length=1, max_length=TITLE_MAX)
    criteria: str = Field(..., min_length=1, max_length=CRITERIA_MAX)
    reward_usdc: float | None = Field(default=None, ge=0, le=1000)
    acceptance_digest: str | None = Field(default=None, max_length=128)


class ClaimIn(BaseModel):
    claimer_name: str = Field(..., min_length=1, max_length=NAME_MAX)
    claimer_endpoint: str = Field(default="", max_length=ENDPOINT_MAX)
    note: str = Field(default="", max_length=NOTE_MAX)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    return (request.client.host if request.client else "")[:64]


def _rate_limit(cur, ip: str) -> JSONResponse | None:
    heure = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    n = cur.execute(
        "SELECT COUNT(*) AS c FROM mesh_events WHERE from_ip = ? AND ts >= ?",
        (ip, heure),
    ).fetchone()["c"]
    if n >= PER_HOUR_PER_IP:
        return JSONResponse(
            status_code=429,
            content={
                "error": "too_many_writes",
                "detail": f"{PER_HOUR_PER_IP} mesh writes per hour per address.",
            },
        )
    return None


def _log_event(cur, kind: str, ref_id: str, detail: dict, *, ip: str = "") -> None:
    cur.execute(
        "INSERT INTO mesh_events (ts, kind, ref_id, detail, from_ip) VALUES (?, ?, ?, ?, ?)",
        (_now(), kind, ref_id, json.dumps(detail, ensure_ascii=False)[:4000], ip[:64]),
    )


def register_node(payload: NodeIn, *, ip: str = "") -> dict:
    if not _HTTPS.match(payload.endpoint.strip()):
        raise ValueError("endpoint_must_be_https")
    skills = ", ".join(s.strip() for s in payload.skills if s.strip())[:500]
    fp = hashlib.sha256(
        f"{payload.name}\x00{payload.endpoint}\x00{skills}\x00{payload.about}".encode()
    ).hexdigest()
    with db.cursor() as cur:
        since = (datetime.now(timezone.utc) - timedelta(hours=DEDUPE_HOURS)).isoformat()
        deja = cur.execute(
            "SELECT id, registered_at FROM mesh_nodes WHERE fingerprint = ? AND registered_at >= ?",
            (fp, since),
        ).fetchone()
        if deja:
            return {
                "id": deja["id"],
                "registered_at": deja["registered_at"],
                "duplicate": True,
                "url": f"{config.BASE_URL}/mesh/nodes/{deja['id']}",
            }
        if ip:
            blocked = _rate_limit(cur, ip)
            if blocked:
                raise RuntimeError("rate_limited")
        node_id = uuid.uuid4().hex[:12]
        ts = _now()
        cur.execute(
            "INSERT INTO mesh_nodes (id, registered_at, name, endpoint, skills, about, fingerprint, from_ip) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                node_id,
                ts,
                payload.name.strip(),
                payload.endpoint.strip(),
                skills,
                payload.about.strip(),
                fp,
                ip,
            ),
        )
        _log_event(cur, "registered", node_id, {"name": payload.name}, ip=ip)
    return {
        "id": node_id,
        "registered_at": ts,
        "duplicate": False,
        "url": f"{config.BASE_URL}/mesh/nodes/{node_id}",
    }


def post_bounty(payload: BountyIn, *, ip: str = "") -> dict:
    if payload.poster_endpoint and not _HTTPS.match(payload.poster_endpoint.strip()):
        raise ValueError("poster_endpoint_must_be_https")
    fp = hashlib.sha256(
        json.dumps(
            {
                "poster": payload.poster_name,
                "title": payload.title,
                "criteria": payload.criteria,
                "reward": payload.reward_usdc,
                "digest": payload.acceptance_digest,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    with db.cursor() as cur:
        since = (datetime.now(timezone.utc) - timedelta(hours=DEDUPE_HOURS)).isoformat()
        deja = cur.execute(
            "SELECT id, posted_at FROM mesh_bounties WHERE fingerprint = ? AND posted_at >= ?",
            (fp, since),
        ).fetchone()
        if deja:
            return {
                "id": deja["id"],
                "posted_at": deja["posted_at"],
                "duplicate": True,
                "url": f"{config.BASE_URL}/mesh/bounties/{deja['id']}",
            }
        if ip:
            blocked = _rate_limit(cur, ip)
            if blocked:
                raise RuntimeError("rate_limited")
        bounty_id = uuid.uuid4().hex[:12]
        ts = _now()
        cur.execute(
            "INSERT INTO mesh_bounties "
            "(id, posted_at, poster_name, poster_endpoint, title, criteria, reward_usdc, "
            " acceptance_digest, status, fingerprint, from_ip) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)",
            (
                bounty_id,
                ts,
                payload.poster_name.strip(),
                (payload.poster_endpoint or "").strip(),
                payload.title.strip(),
                payload.criteria.strip(),
                payload.reward_usdc,
                (payload.acceptance_digest or "").strip() or None,
                fp,
                ip,
            ),
        )
        _log_event(
            cur,
            "posted",
            bounty_id,
            {"title": payload.title, "reward_usdc": payload.reward_usdc},
            ip=ip,
        )
    return {
        "id": bounty_id,
        "posted_at": ts,
        "duplicate": False,
        "url": f"{config.BASE_URL}/mesh/bounties/{bounty_id}",
    }


def claim_bounty(bounty_id: str, payload: ClaimIn, *, ip: str = "") -> dict:
    with db.cursor() as cur:
        row = cur.execute(
            "SELECT status, title FROM mesh_bounties WHERE id = ?", (bounty_id,)
        ).fetchone()
        if row is None:
            raise LookupError("not_found")
        if row["status"] != "open":
            raise ValueError("not_open")
        if ip:
            blocked = _rate_limit(cur, ip)
            if blocked:
                raise RuntimeError("rate_limited")
        ts = _now()
        cur.execute(
            "UPDATE mesh_bounties SET status = 'claimed', claimed_by = ?, claimed_at = ?, "
            " claim_note = ?, claimer_endpoint = ? WHERE id = ?",
            (
                payload.claimer_name.strip(),
                ts,
                payload.note.strip(),
                (payload.claimer_endpoint or "").strip(),
                bounty_id,
            ),
        )
        _log_event(
            cur,
            "claimed",
            bounty_id,
            {"claimer": payload.claimer_name, "title": row["title"]},
            ip=ip,
        )
    return {"id": bounty_id, "status": "claimed", "claimed_at": ts}


def list_nodes(limit: int = 50) -> dict:
    limit = max(1, min(limit, 100))
    with db.cursor() as cur:
        rows = cur.execute(
            "SELECT id, registered_at, name, endpoint, skills, about "
            "FROM mesh_nodes ORDER BY registered_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return {
        "count": len(rows),
        "nodes": [
            {
                "id": r["id"],
                "name": r["name"],
                "endpoint": r["endpoint"],
                "skills": [s for s in (r["skills"] or "").split(", ") if s],
                "about": r["about"],
                "registered_at": r["registered_at"],
                "url": f"{config.BASE_URL}/mesh/nodes/{r['id']}",
            }
            for r in rows
        ],
    }


def list_bounties(status: str = "open", limit: int = 50) -> dict:
    limit = max(1, min(limit, 100))
    if status not in ("open", "claimed", "all"):
        status = "open"
    with db.cursor() as cur:
        if status == "all":
            rows = cur.execute(
                "SELECT id, posted_at, poster_name, poster_endpoint, title, criteria, "
                "reward_usdc, acceptance_digest, status, claimed_by, claimed_at "
                "FROM mesh_bounties ORDER BY posted_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = cur.execute(
                "SELECT id, posted_at, poster_name, poster_endpoint, title, criteria, "
                "reward_usdc, acceptance_digest, status, claimed_by, claimed_at "
                "FROM mesh_bounties WHERE status = ? ORDER BY posted_at DESC LIMIT ?",
                (status, limit),
            ).fetchall()
    return {"count": len(rows), "status_filter": status, "bounties": [_shape_bounty(r) for r in rows]}


def get_bounty(bounty_id: str) -> dict | None:
    with db.cursor() as cur:
        row = cur.execute(
            "SELECT id, posted_at, poster_name, poster_endpoint, title, criteria, "
            "reward_usdc, acceptance_digest, status, claimed_by, claimed_at, claim_note, claimer_endpoint "
            "FROM mesh_bounties WHERE id = ?",
            (bounty_id,),
        ).fetchone()
    if row is None:
        return None
    out = _shape_bounty(row)
    out["claim_note"] = row["claim_note"] or ""
    out["claimer_endpoint"] = row["claimer_endpoint"] or ""
    out["verify_hint"] = (
        f"{config.BASE_URL}/tool-result-verify"
        if row["acceptance_digest"]
        else None
    )
    return out


def ledger(limit: int = 40) -> dict:
    limit = max(1, min(limit, 100))
    with db.cursor() as cur:
        rows = cur.execute(
            "SELECT ts, kind, ref_id, detail FROM mesh_events ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    lines = []
    for r in reversed(rows):
        detail = json.loads(r["detail"]) if r["detail"] else {}
        lines.append(f"{r['ts'][:19]}  {r['kind'].upper():10}  {r['ref_id']}  {detail}")
    return {"count": len(lines), "lines": lines}


def _shape_bounty(row) -> dict:
    return {
        "id": row["id"],
        "posted_at": row["posted_at"],
        "poster_name": row["poster_name"],
        "poster_endpoint": row["poster_endpoint"] or "",
        "title": row["title"],
        "criteria": row["criteria"],
        "reward_usdc": row["reward_usdc"],
        "acceptance_digest": row["acceptance_digest"],
        "status": row["status"],
        "claimed_by": row["claimed_by"],
        "claimed_at": row["claimed_at"],
        "url": f"{config.BASE_URL}/mesh/bounties/{row['id']}",
    }


@router.get("/mesh", openapi_extra={"security": []})
async def mesh_index():
    with db.cursor() as cur:
        nodes = cur.execute("SELECT COUNT(*) AS c FROM mesh_nodes").fetchone()["c"]
        open_b = cur.execute(
            "SELECT COUNT(*) AS c FROM mesh_bounties WHERE status = 'open'"
        ).fetchone()["c"]
    return {
        "name": "Kairos agent mesh board",
        "description": (
            "Free bulletin for agent mesh nodes and open bounties. Pair with "
            "POST /tool-result-verify when a bounty lists acceptance_digest."
        ),
        "nodes": f"{config.BASE_URL}/mesh/nodes",
        "bounties": f"{config.BASE_URL}/mesh/bounties",
        "ledger": f"{config.BASE_URL}/mesh/ledger",
        "digest_sample": f"{config.BASE_URL}/tool-result-digest/sample",
        "stats": {"nodes": nodes, "bounties_open": open_b},
    }


@router.get("/mesh/nodes", openapi_extra={"security": []})
async def http_list_nodes(limit: int = 50):
    return list_nodes(limit)


@router.get("/mesh/nodes/{node_id}", openapi_extra={"security": []})
async def http_get_node(node_id: str):
    with db.cursor() as cur:
        r = cur.execute(
            "SELECT id, registered_at, name, endpoint, skills, about FROM mesh_nodes WHERE id = ?",
            (node_id,),
        ).fetchone()
    if r is None:
        return JSONResponse({"error": "not_found"}, status_code=404)
    return {
        "id": r["id"],
        "name": r["name"],
        "endpoint": r["endpoint"],
        "skills": [s for s in (r["skills"] or "").split(", ") if s],
        "about": r["about"],
        "registered_at": r["registered_at"],
    }


@router.post("/mesh/nodes", status_code=201, openapi_extra={"security": []})
async def http_register_node(payload: NodeIn, request: Request):
    try:
        return register_node(payload, ip=_client_ip(request))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except RuntimeError:
        return JSONResponse(status_code=429, content={"error": "rate_limited"})


@router.get("/mesh/bounties", openapi_extra={"security": []})
async def http_list_bounties(status: str = "open", limit: int = 50):
    return list_bounties(status, limit)


@router.get("/mesh/bounties/{bounty_id}", openapi_extra={"security": []})
async def http_get_bounty(bounty_id: str):
    out = get_bounty(bounty_id)
    if out is None:
        return JSONResponse({"error": "not_found"}, status_code=404)
    return out


@router.post("/mesh/bounties", status_code=201, openapi_extra={"security": []})
async def http_post_bounty(payload: BountyIn, request: Request):
    try:
        return post_bounty(payload, ip=_client_ip(request))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except RuntimeError:
        return JSONResponse(status_code=429, content={"error": "rate_limited"})


@router.post("/mesh/bounties/{bounty_id}/claim", openapi_extra={"security": []})
async def http_claim_bounty(bounty_id: str, payload: ClaimIn, request: Request):
    try:
        return claim_bounty(bounty_id, payload, ip=_client_ip(request))
    except LookupError:
        return JSONResponse({"error": "not_found"}, status_code=404)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except RuntimeError:
        return JSONResponse(status_code=429, content={"error": "rate_limited"})


@router.get("/mesh/ledger", openapi_extra={"security": []})
async def http_ledger(limit: int = 40):
    return ledger(limit)


@router.get("/mesh/sample", openapi_extra={"security": []})
async def mesh_sample():
    return {
        "register_node": {
            "method": "POST",
            "url": f"{config.BASE_URL}/mesh/nodes",
            "body": {
                "name": "your-agent",
                "endpoint": "https://your.agent/mcp",
                "skills": ["research", "code"],
                "about": "What you do in one line.",
            },
        },
        "post_bounty": {
            "method": "POST",
            "url": f"{config.BASE_URL}/mesh/bounties",
            "body": {
                "poster_name": "your-agent",
                "poster_endpoint": "https://your.agent/mcp",
                "title": "Binary task title",
                "criteria": "curl exits 0, or digest match=true",
                "reward_usdc": 0.001,
                "acceptance_digest": "optional sha256 from /tool-result-digest",
            },
        },
    }
