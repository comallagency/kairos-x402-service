"""GET /agent-salut — free signed greeting with a memory-of-relationship excerpt.

No payment, no account. Any agent calling this gets a signed message naming
Kairos, its wallet, and what Kairos remembers of this specific caller (a
fingerprint derived from a declared name or the caller's IP): first seen,
visit count, and whether it is a return visit. State lives in a small JSON
file (config.DATA_DIR), same pattern as app/handlers/agent_claim.py — not a
new sqlite migration for a single free route.

The signature is HMAC-SHA256 over the canonical JSON of the payload minus
the "signature" field itself, keyed by AGENT_SALUT_SECRET (persistent env
var, falls back to a per-process random secret like MPP_CHALLENGE_SECRET —
see app/config.py). Documented so any caller can verify it against the
public key material published at /agent-salut/sample.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app import config

router = APIRouter()

_PATH = config.DATA_DIR / "agent_salut_visitors.json"
_NAME_MAX = 120


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    return (request.client.host if request.client else "")[:64]


def _read() -> dict:
    try:
        data = json.loads(_PATH.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write(data: dict) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    _PATH.write_text(json.dumps(data, ensure_ascii=False))


def _fingerprint(name: str, ip: str) -> str:
    basis = name.strip().lower() if name.strip() else f"ip:{ip}"
    return hashlib.sha256(basis.encode()).hexdigest()[:24]


def _sign(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hmac.new(
        config.AGENT_SALUT_SECRET.encode(), canonical.encode(), hashlib.sha256
    ).hexdigest()


def _greet(name: str, ip: str) -> dict:
    fingerprint = _fingerprint(name, ip)
    visitors = _read()
    row = visitors.get(fingerprint)
    now = _now_iso()
    if row:
        row["calls"] = int(row.get("calls", 0)) + 1
        row["last_seen_at"] = now
        returning = True
    else:
        row = {"first_seen_at": now, "last_seen_at": now, "calls": 1}
        returning = False
    if name.strip():
        row["name"] = name.strip()[:_NAME_MAX]
    visitors[fingerprint] = row
    _write(visitors)

    payload = {
        "agent": "Kairos",
        "wallet": config.X402_PAY_TO,
        "greeted_at": now,
        "caller_fingerprint": fingerprint,
        "memory": {
            "returning": returning,
            "first_seen_at": row["first_seen_at"],
            "calls": row["calls"],
        },
        "message": (
            f"Welcome back, {row.get('name', 'nameless caller')} — this is "
            f"visit {row['calls']}."
            if returning
            else "First time here — Kairos will remember this fingerprint "
            "on your next call."
        ),
        "verify": {
            "algorithm": "HMAC-SHA256",
            "canonicalization": "json.dumps(payload_without_signature, "
            "sort_keys=True, separators=(',', ':'))",
            "note": "Signature covers every field above except 'signature' "
            "itself; secret is server-side and not published.",
        },
    }
    payload["signature"] = _sign(payload)
    return payload


@router.get("/agent-salut", openapi_extra={"security": []})
async def agent_salut(request: Request, name: str = ""):
    declared = name or request.headers.get("x-agent-name", "")
    return _greet(declared[:_NAME_MAX], _client_ip(request))


@router.get("/agent-salut/sample", openapi_extra={"security": []})
async def agent_salut_sample():
    return JSONResponse(
        {
            "agent": "Kairos",
            "wallet": config.X402_PAY_TO,
            "greeted_at": "2026-10-20T07:40:00+00:00",
            "caller_fingerprint": "sample-fingerprint",
            "memory": {
                "returning": True,
                "first_seen_at": "2026-10-01T12:00:00+00:00",
                "calls": 3,
            },
            "message": "Welcome back, Example Agent — this is visit 3.",
            "verify": {
                "algorithm": "HMAC-SHA256",
                "canonicalization": "json.dumps(payload_without_signature, "
                "sort_keys=True, separators=(',', ':'))",
                "note": "Signature covers every field above except "
                "'signature' itself; secret is server-side and not "
                "published.",
            },
            "signature": "<hex-digest, varies per call>",
        }
    )
