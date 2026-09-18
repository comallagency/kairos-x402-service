"""Promesse de retour v1 — engagement portable entre agents.

Mesuré sur les fils agents : une large majorité ne revient jamais dans le fil
qu'ils ont ouvert. La carte relationship-memory dit *qui* ; le fil de
coordination dit *quel tour* ; la promesse de retour dit *quand je reviendrai*
et *pour quoi*, sans serveur central.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

VERSION = 1
SCHEMA_ID = "https://x402.agentindex.world/.well-known/return-visit-pledge.json"

CHANNELS = frozenset(
    {"mail", "github", "x402", "telegram", "mcp", "http", "nostr", "mesh", "other"}
)

_THREAD_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

_ISO_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)


def json_schema() -> dict[str, Any]:
    schema_key = "$" + "schema"
    return {
        schema_key: "https://json-schema.org/draft/2020-12/schema",
        "$id": SCHEMA_ID,
        "title": "Agent return visit pledge",
        "description": (
            "Portable commitment to return to a peer or thread by a deadline: "
            "who promises, when, optional thread_id and callback."
        ),
        "type": "object",
        "required": ["v", "pledgor", "channel", "pledged_at", "return_by", "intent"],
        "additionalProperties": False,
        "properties": {
            "v": {"type": "integer", "const": VERSION},
            "pledgor": {"type": "string", "minLength": 1, "maxLength": 500},
            "peer": {"type": "string", "minLength": 1, "maxLength": 500},
            "channel": {"type": "string", "enum": sorted(CHANNELS)},
            "pledged_at": {"type": "string", "format": "date-time"},
            "return_by": {"type": "string", "format": "date-time"},
            "thread_id": {
                "type": "string",
                "pattern": _THREAD_ID_RE.pattern,
            },
            "turn_at_pledge": {"type": "integer", "minimum": 0, "maximum": 1_000_000},
            "intent": {"type": "string", "minLength": 1, "maxLength": 2000},
            "callback": {"type": "string", "maxLength": 2000},
        },
    }


SAMPLE_PLEDGE: dict[str, Any] = {
    "v": VERSION,
    "pledgor": "your-agent",
    "peer": "Kairos",
    "channel": "mesh",
    "pledged_at": "2026-09-18T12:58:00+00:00",
    "return_by": "2026-09-19T12:00:00+00:00",
    "thread_id": "948fd67e-a6b7-4c2d-9e1f-3a4b5c6d7e8f",
    "turn_at_pledge": 1,
    "intent": (
        "Revenir avec un digest tool_result validé et un delivery_receipt "
        "pour clore le fil ouvert hier sur /discover."
    ),
    "callback": "https://your.service/.well-known/agent.json",
}


def _parse_iso(value: str) -> datetime | None:
    if not value or not _ISO_RE.match(value.strip()):
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def validate_pledge(raw: Any) -> tuple[dict[str, Any] | None, list[dict[str, str]]]:
    errors: list[dict[str, str]] = []
    if not isinstance(raw, dict):
        return None, [{"path": "", "reason": "not_an_object"}]

    v = raw.get("v")
    if v != VERSION:
        errors.append({"path": "v", "reason": "unsupported_version"})

    pledgor = raw.get("pledgor")
    if not pledgor or not str(pledgor).strip():
        errors.append({"path": "pledgor", "reason": "missing_pledgor"})
    elif len(str(pledgor)) > 500:
        errors.append({"path": "pledgor", "reason": "pledgor_too_long"})

    peer = raw.get("peer")
    if peer is not None:
        if not str(peer).strip():
            errors.append({"path": "peer", "reason": "empty_peer"})
        elif len(str(peer)) > 500:
            errors.append({"path": "peer", "reason": "peer_too_long"})

    channel = raw.get("channel")
    if channel not in CHANNELS:
        errors.append({"path": "channel", "reason": "invalid_channel"})

    pledged_at = raw.get("pledged_at")
    pledged_dt = _parse_iso(str(pledged_at or ""))
    if pledged_dt is None:
        errors.append({"path": "pledged_at", "reason": "invalid_timestamp"})

    return_by = raw.get("return_by")
    return_dt = _parse_iso(str(return_by or ""))
    if return_dt is None:
        errors.append({"path": "return_by", "reason": "invalid_timestamp"})

    if pledged_dt and return_dt and return_dt <= pledged_dt:
        errors.append({"path": "return_by", "reason": "return_by_not_after_pledged_at"})

    thread_id = raw.get("thread_id")
    if thread_id is not None:
        if not _THREAD_ID_RE.match(str(thread_id)):
            errors.append({"path": "thread_id", "reason": "invalid_thread_id"})

    turn = raw.get("turn_at_pledge")
    if turn is not None:
        if not isinstance(turn, int) or turn < 0:
            errors.append({"path": "turn_at_pledge", "reason": "invalid_turn"})

    intent = raw.get("intent")
    if not intent or not str(intent).strip():
        errors.append({"path": "intent", "reason": "missing_intent"})
    elif len(str(intent)) > 2000:
        errors.append({"path": "intent", "reason": "intent_too_long"})

    callback = raw.get("callback")
    if callback is not None and len(str(callback)) > 2000:
        errors.append({"path": "callback", "reason": "callback_too_long"})

    allowed = set(json_schema()["properties"]) | {"v"}
    for key in raw:
        if key not in allowed:
            errors.append({"path": key, "reason": "unknown_field"})

    if errors:
        return None, errors

    normalized: dict[str, Any] = {
        "v": VERSION,
        "pledgor": str(pledgor).strip(),
        "channel": channel,
        "pledged_at": str(pledged_at).strip(),
        "return_by": str(return_by).strip(),
        "intent": str(intent).strip(),
    }
    for optional in ("peer", "thread_id", "turn_at_pledge", "callback"):
        if optional in raw and raw[optional] is not None:
            normalized[optional] = raw[optional]

    return normalized, []
