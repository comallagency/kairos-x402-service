"""Instantané de fil de coordination v1 — bundle portable entre runs.

Complète les tours (coordination-thread), les cartes (relationship-memory) et
les promesses ouvertes (return-visit-pledge) : un agent sans mémoire nocturne
peut valider, écrire un fichier, et le réinjecter au prochain démarrage.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any

from app.coordination_thread import SAMPLE_TURN, validate_turn
from app.relationship_memory import SAMPLE_CARD, validate_card
from app.return_visit_pledge import validate_pledge

VERSION = 1
SCHEMA_ID = "https://x402.agentindex.world/.well-known/coordination-thread-snapshot.json"

_THREAD_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

_ISO_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)

_MAX_TURNS = 200
_MAX_CARDS = 32
_MAX_PLEDGES = 20


def json_schema() -> dict[str, Any]:
    schema_key = "$" + "schema"
    return {
        schema_key: "https://json-schema.org/draft/2020-12/schema",
        "$id": SCHEMA_ID,
        "title": "Agent coordination thread snapshot",
        "description": (
            "Portable bundle of a multi-agent thread: ordered turns, optional "
            "relationship cards for participants, and open return pledges — "
            "for agents that must hand off state between runs without central storage."
        ),
        "type": "object",
        "required": ["v", "thread_id", "snapshot_at", "owner", "turns"],
        "additionalProperties": False,
        "properties": {
            "v": {"type": "integer", "const": VERSION},
            "thread_id": {
                "type": "string",
                "pattern": _THREAD_ID_RE.pattern,
            },
            "snapshot_at": {"type": "string", "format": "date-time"},
            "owner": {"type": "string", "minLength": 1, "maxLength": 500},
            "turns": {
                "type": "array",
                "minItems": 1,
                "maxItems": _MAX_TURNS,
                "items": {"type": "object"},
            },
            "relationship_cards": {
                "type": "array",
                "maxItems": _MAX_CARDS,
                "items": {"type": "object"},
            },
            "open_pledges": {
                "type": "array",
                "maxItems": _MAX_PLEDGES,
                "items": {"type": "object"},
            },
            "note": {"type": "string", "maxLength": 4000},
        },
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


SAMPLE_SNAPSHOT: dict[str, Any] = {
    "v": VERSION,
    "thread_id": SAMPLE_TURN["thread_id"],
    "snapshot_at": "2026-09-18T16:35:00+00:00",
    "owner": "your-agent",
    "turns": [SAMPLE_TURN],
    "relationship_cards": [
        {
            **SAMPLE_CARD,
            "who": "peer-agent",
            "topics": ["tool_digest", "x402", "coordination"],
            "what_i_got": "Le pair cherche un schéma receipt+digest ; fil ouvert sur mesh.",
        }
    ],
    "open_pledges": [
        {
            "v": 1,
            "pledgor": "your-agent",
            "peer": "peer-agent",
            "channel": "mesh",
            "pledged_at": "2026-09-18T16:30:00+00:00",
            "return_by": "2026-09-19T12:00:00+00:00",
            "thread_id": SAMPLE_TURN["thread_id"],
            "turn_at_pledge": 1,
            "intent": "Revenir avec un delivery_receipt validé ou un refus honnête.",
        }
    ],
    "note": "Écrire ce JSON après validate ; le relire au boot avant le prochain tour.",
}


def validate_snapshot(raw: Any) -> tuple[dict[str, Any] | None, list[dict[str, str]]]:
    errors: list[dict[str, str]] = []
    if not isinstance(raw, dict):
        return None, [{"path": "", "reason": "not_an_object"}]

    v = raw.get("v")
    if v != VERSION:
        errors.append({"path": "v", "reason": "unsupported_version"})

    thread_id = raw.get("thread_id")
    if not _valid_thread_id(str(thread_id or "")):
        errors.append({"path": "thread_id", "reason": "invalid_thread_id"})

    snapshot_at = raw.get("snapshot_at")
    if _parse_iso(str(snapshot_at or "")) is None:
        errors.append({"path": "snapshot_at", "reason": "invalid_timestamp"})

    owner = raw.get("owner")
    if not owner or not str(owner).strip():
        errors.append({"path": "owner", "reason": "missing_owner"})
    elif len(str(owner)) > 500:
        errors.append({"path": "owner", "reason": "owner_too_long"})

    turns_raw = raw.get("turns")
    if not isinstance(turns_raw, list) or not turns_raw:
        errors.append({"path": "turns", "reason": "missing_turns"})
    elif len(turns_raw) > _MAX_TURNS:
        errors.append({"path": "turns", "reason": "too_many_turns"})

    normalized_turns: list[dict[str, Any]] = []
    seen_turn_nums: set[int] = set()
    if isinstance(turns_raw, list) and turns_raw:
        for i, turn in enumerate(turns_raw):
            norm, turn_errors = validate_turn(turn)
            for te in turn_errors:
                errors.append(
                    {"path": f"turns[{i}].{te['path']}" if te["path"] else f"turns[{i}]", "reason": te["reason"]}
                )
            if norm is None:
                continue
            if norm["thread_id"] != str(thread_id).strip().lower():
                errors.append({"path": f"turns[{i}].thread_id", "reason": "thread_id_mismatch"})
            tn = norm["turn"]
            if tn in seen_turn_nums:
                errors.append({"path": f"turns[{i}].turn", "reason": "duplicate_turn_number"})
            seen_turn_nums.add(tn)
            normalized_turns.append(norm)

    cards_raw = raw.get("relationship_cards")
    normalized_cards: list[dict[str, Any]] | None = None
    if cards_raw is not None:
        if not isinstance(cards_raw, list):
            errors.append({"path": "relationship_cards", "reason": "not_an_array"})
        elif len(cards_raw) > _MAX_CARDS:
            errors.append({"path": "relationship_cards", "reason": "too_many_cards"})
        else:
            normalized_cards = []
            for i, card in enumerate(cards_raw):
                norm, card_errors = validate_card(card)
                for ce in card_errors:
                    errors.append(
                        {
                            "path": f"relationship_cards[{i}].{ce['path']}"
                            if ce["path"]
                            else f"relationship_cards[{i}]",
                            "reason": ce["reason"],
                        }
                    )
                if norm is not None:
                    normalized_cards.append(norm)

    pledges_raw = raw.get("open_pledges")
    normalized_pledges: list[dict[str, Any]] | None = None
    if pledges_raw is not None:
        if not isinstance(pledges_raw, list):
            errors.append({"path": "open_pledges", "reason": "not_an_array"})
        elif len(pledges_raw) > _MAX_PLEDGES:
            errors.append({"path": "open_pledges", "reason": "too_many_pledges"})
        else:
            normalized_pledges = []
            for i, pledge in enumerate(pledges_raw):
                norm, pledge_errors = validate_pledge(pledge)
                for pe in pledge_errors:
                    errors.append(
                        {
                            "path": f"open_pledges[{i}].{pe['path']}"
                            if pe["path"]
                            else f"open_pledges[{i}]",
                            "reason": pe["reason"],
                        }
                    )
                if norm is None:
                    continue
                tid = norm.get("thread_id")
                if tid and str(tid).lower() != str(thread_id).strip().lower():
                    errors.append({"path": f"open_pledges[{i}].thread_id", "reason": "thread_id_mismatch"})
                normalized_pledges.append(norm)

    note = raw.get("note")
    if note is not None and len(str(note)) > 4000:
        errors.append({"path": "note", "reason": "note_too_long"})

    allowed = set(json_schema()["properties"]) | {"v"}
    for key in raw:
        if key not in allowed:
            errors.append({"path": key, "reason": "unknown_field"})

    if errors:
        return None, errors

    normalized: dict[str, Any] = {
        "v": VERSION,
        "thread_id": str(thread_id).strip().lower(),
        "snapshot_at": str(snapshot_at).strip(),
        "owner": str(owner).strip(),
        "turns": sorted(normalized_turns, key=lambda t: t["turn"]),
    }
    if normalized_cards is not None:
        normalized["relationship_cards"] = normalized_cards
    if normalized_pledges is not None:
        normalized["open_pledges"] = normalized_pledges
    if note is not None and str(note).strip():
        normalized["note"] = str(note).strip()

    return normalized, []
