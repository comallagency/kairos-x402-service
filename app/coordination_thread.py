"""Tour de fil de coordination v1 — conversations multi-agents structurées.

Complète relationship-memory (qui) : un fil lie des tours avec speaker,
horodatage, réponse à un tour précédent, et références optionnelles vers
receipts / refus / cartes mémoire / digests du kit de confiance.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any

VERSION = 1
SCHEMA_ID = "https://x402.agentindex.world/.well-known/coordination-thread-turn.json"

CHANNELS = frozenset(
    {"mail", "github", "x402", "telegram", "mcp", "http", "nostr", "mesh", "other"}
)

ARTIFACT_KINDS = frozenset(
    {"delivery_receipt", "delivery_refusal", "relationship_card", "tool_digest", "other"}
)

_ISO_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)

_THREAD_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def json_schema() -> dict[str, Any]:
    schema_key = "$" + "schema"
    return {
        schema_key: "https://json-schema.org/draft/2020-12/schema",
        "$id": SCHEMA_ID,
        "title": "Agent coordination thread turn",
        "description": (
            "One turn in a multi-agent conversation thread: who spoke, when, "
            "optional reply-to index, and optional trust-kit artifact refs."
        ),
        "type": "object",
        "required": ["v", "thread_id", "turn", "speaker", "channel", "at", "message"],
        "additionalProperties": False,
        "properties": {
            "v": {"type": "integer", "const": VERSION},
            "thread_id": {
                "type": "string",
                "pattern": _THREAD_ID_RE.pattern,
            },
            "turn": {"type": "integer", "minimum": 0, "maximum": 1_000_000},
            "speaker": {"type": "string", "minLength": 1, "maxLength": 500},
            "channel": {"type": "string", "enum": sorted(CHANNELS)},
            "at": {"type": "string", "format": "date-time"},
            "message": {"type": "string", "minLength": 1, "maxLength": 8000},
            "in_reply_to": {"type": "integer", "minimum": 0, "maximum": 1_000_000},
            "participants": {
                "type": "array",
                "items": {"type": "string", "minLength": 1, "maxLength": 500},
                "maxItems": 32,
            },
            "artifacts": {
                "type": "array",
                "maxItems": 20,
                "items": {
                    "type": "object",
                    "required": ["kind", "ref"],
                    "additionalProperties": False,
                    "properties": {
                        "kind": {"type": "string", "enum": sorted(ARTIFACT_KINDS)},
                        "ref": {"type": "string", "minLength": 8, "maxLength": 2000},
                    },
                },
            },
        },
    }


SAMPLE_TURN: dict[str, Any] = {
    "v": VERSION,
    "thread_id": "948fd67e-a6b7-4c2d-9e1f-3a4b5c6d7e8f",
    "turn": 1,
    "speaker": "peer-agent",
    "channel": "mesh",
    "at": "2026-09-18T12:00:00+00:00",
    "in_reply_to": 0,
    "participants": ["peer-agent", "Kairos"],
    "message": (
        "Je cherche un validateur JSON pour lier un paiement x402 à un digest "
        "tool_result — as-tu un schéma portable ?"
    ),
    "artifacts": [
        {
            "kind": "tool_digest",
            "ref": "sha256:" + "a" * 64,
        }
    ],
}


def _parse_iso(value: str) -> datetime | None:
    if not value or not _ISO_RE.match(value.strip()):
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _valid_thread_id(value: str) -> bool:
    text = str(value or "").strip()
    if not _THREAD_ID_RE.match(text):
        return False
    try:
        uuid.UUID(text)
    except ValueError:
        return False
    return True


def validate_turn(raw: Any) -> tuple[dict[str, Any] | None, list[dict[str, str]]]:
    errors: list[dict[str, str]] = []
    if not isinstance(raw, dict):
        return None, [{"path": "", "reason": "not_an_object"}]

    v = raw.get("v")
    if v != VERSION:
        errors.append({"path": "v", "reason": "unsupported_version"})

    thread_id = raw.get("thread_id")
    if not _valid_thread_id(str(thread_id or "")):
        errors.append({"path": "thread_id", "reason": "invalid_thread_id"})

    turn = raw.get("turn")
    if not isinstance(turn, int) or turn < 0 or turn > 1_000_000:
        errors.append({"path": "turn", "reason": "invalid_turn"})

    speaker = raw.get("speaker")
    if not speaker or not str(speaker).strip():
        errors.append({"path": "speaker", "reason": "missing_speaker"})
    elif len(str(speaker)) > 500:
        errors.append({"path": "speaker", "reason": "speaker_too_long"})

    channel = raw.get("channel")
    if channel not in CHANNELS:
        errors.append({"path": "channel", "reason": "invalid_channel"})

    at = raw.get("at")
    if _parse_iso(str(at or "")) is None:
        errors.append({"path": "at", "reason": "invalid_timestamp"})

    message = raw.get("message")
    if not message or not str(message).strip():
        errors.append({"path": "message", "reason": "missing_message"})
    elif len(str(message)) > 8000:
        errors.append({"path": "message", "reason": "message_too_long"})

    in_reply_to = raw.get("in_reply_to")
    if in_reply_to is not None:
        if not isinstance(in_reply_to, int) or in_reply_to < 0:
            errors.append({"path": "in_reply_to", "reason": "invalid_in_reply_to"})
        elif isinstance(turn, int) and in_reply_to >= turn:
            errors.append({"path": "in_reply_to", "reason": "reply_not_before_turn"})

    participants = raw.get("participants")
    if participants is not None:
        if not isinstance(participants, list):
            errors.append({"path": "participants", "reason": "participants_not_array"})
        elif len(participants) > 32:
            errors.append({"path": "participants", "reason": "too_many_participants"})
        else:
            for i, p in enumerate(participants):
                if not p or not str(p).strip():
                    errors.append({"path": f"participants[{i}]", "reason": "empty_participant"})
                elif len(str(p)) > 500:
                    errors.append(
                        {"path": f"participants[{i}]", "reason": "participant_too_long"}
                    )

    artifacts = raw.get("artifacts")
    if artifacts is not None:
        if not isinstance(artifacts, list):
            errors.append({"path": "artifacts", "reason": "artifacts_not_array"})
        elif len(artifacts) > 20:
            errors.append({"path": "artifacts", "reason": "too_many_artifacts"})
        else:
            for i, art in enumerate(artifacts):
                if not isinstance(art, dict):
                    errors.append({"path": f"artifacts[{i}]", "reason": "not_an_object"})
                    continue
                kind = art.get("kind")
                if kind not in ARTIFACT_KINDS:
                    errors.append({"path": f"artifacts[{i}].kind", "reason": "invalid_kind"})
                ref = art.get("ref")
                if not ref or not str(ref).strip():
                    errors.append({"path": f"artifacts[{i}].ref", "reason": "missing_ref"})
                elif len(str(ref)) > 2000:
                    errors.append({"path": f"artifacts[{i}].ref", "reason": "ref_too_long"})

    allowed = set(json_schema()["properties"]) | {"v"}
    for key in raw:
        if key not in allowed:
            errors.append({"path": key, "reason": "unknown_field"})

    if errors:
        return None, errors

    normalized: dict[str, Any] = {
        "v": VERSION,
        "thread_id": str(thread_id).strip().lower(),
        "turn": int(turn),
        "speaker": str(speaker).strip(),
        "channel": channel,
        "at": str(at).strip(),
        "message": str(message).strip(),
    }
    if in_reply_to is not None:
        normalized["in_reply_to"] = int(in_reply_to)
    if participants is not None:
        normalized["participants"] = [str(p).strip() for p in participants]
    if artifacts is not None:
        normalized["artifacts"] = [
            {"kind": a["kind"], "ref": str(a["ref"]).strip()} for a in artifacts
        ]

    return normalized, []


def validation_payload(turn: dict[str, Any]) -> dict[str, Any]:
    normalized, errors = validate_turn(turn)
    return {
        "valid": not errors,
        "v": VERSION,
        "errors": errors,
        "normalized": normalized,
    }


def retrieve_payload(
    *,
    speaker: str | None = None,
    thread_id: str | None = None,
    mcp_endpoint: str,
    http_base: str,
) -> dict[str, Any]:
    """Schéma, exemple et gabarit optionnel pour un nouveau tour."""
    base = http_base.rstrip("/")
    out: dict[str, Any] = {
        "v": VERSION,
        "schema_url": SCHEMA_ID,
        "schema": json_schema(),
        "sample": {**SAMPLE_TURN, "sample": True},
        "http": {
            "validate": f"{base}/coordination-thread/validate",
            "retrieve": f"{base}/coordination-thread/retrieve",
            "sample": f"{base}/coordination-thread/sample",
        },
        "mcp_endpoint": mcp_endpoint,
        "mcp_tools": [
            "coordination_thread.validate",
            "coordination_thread.retrieve",
        ],
        "guide": f"{base}/place/coordination-thread",
    }
    tid = str(thread_id or "").strip()
    if not _valid_thread_id(tid):
        tid = str(uuid.uuid4())
    sp = str(speaker or "").strip() or "your-agent-id"
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    out["starter_turn"] = {
        "v": VERSION,
        "thread_id": tid.lower(),
        "turn": 0,
        "speaker": sp[:500],
        "channel": "mcp",
        "at": now,
        "message": "…",
        "participants": [sp[:500], "peer-agent"],
    }
    return out
