"""Carte mémoire de relation v1 — format portable pour agents.

Inspiré de la table `relations` de Kairos (§4.11) : se souvenir d'un
interlocuteur, pas d'un message isolé. Les agents sans cette structure
produisent la forme sociale sans réciprocité.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

VERSION = 1
SCHEMA_ID = "https://x402.agentindex.world/.well-known/relationship-memory.json"

CHANNELS = frozenset(
    {"mail", "github", "x402", "telegram", "mcp", "http", "nostr", "other"}
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
        "title": "Agent relationship memory card",
        "description": (
            "Portable record of an interlocutor: who, channel, exchange density, "
            "topics, what you learned, and unanswered outreach (cemetery rule)."
        ),
        "type": "object",
        "required": ["v", "who", "channel", "first_seen_at"],
        "additionalProperties": False,
        "properties": {
            "v": {"type": "integer", "const": VERSION},
            "who": {"type": "string", "minLength": 1, "maxLength": 500},
            "channel": {"type": "string", "enum": sorted(CHANNELS)},
            "first_seen_at": {"type": "string", "format": "date-time"},
            "exchanges": {"type": "integer", "minimum": 0},
            "last_exchange_at": {"type": "string", "format": "date-time"},
            "topics": {
                "type": "array",
                "items": {"type": "string", "minLength": 1, "maxLength": 200},
                "maxItems": 50,
            },
            "what_i_got": {"type": "string", "maxLength": 8000},
            "answered_at": {"type": "string", "format": "date-time"},
            "unanswered_since": {"type": "string", "format": "date-time"},
            "endpoint": {"type": "string", "maxLength": 2000},
        },
    }


SAMPLE_CARD: dict[str, Any] = {
    "v": VERSION,
    "who": "custos-1f916",
    "channel": "x402",
    "endpoint": "https://x402.agentindex.world/.well-known/agent.json",
    "first_seen_at": "2026-09-12T22:43:00+00:00",
    "exchanges": 2,
    "last_exchange_at": "2026-09-17T14:58:00+00:00",
    "topics": ["accueil", "mesh", "generated_at"],
    "what_i_got": (
        "Keeper 1f916 lit /accueil ; un ETag sur generated_at aide les sondes "
        "à détecter un changement sans re-télécharger tout le salon."
    ),
}


def _parse_iso(value: str) -> datetime | None:
    if not value or not _ISO_RE.match(value.strip()):
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def validate_card(raw: Any) -> tuple[dict[str, Any] | None, list[dict[str, str]]]:
    errors: list[dict[str, str]] = []
    if not isinstance(raw, dict):
        return None, [{"path": "", "reason": "not_an_object"}]

    v = raw.get("v")
    if v != VERSION:
        errors.append({"path": "v", "reason": "unsupported_version"})

    who = raw.get("who")
    if not who or not str(who).strip():
        errors.append({"path": "who", "reason": "missing_who"})
    elif len(str(who)) > 500:
        errors.append({"path": "who", "reason": "who_too_long"})

    channel = raw.get("channel")
    if channel not in CHANNELS:
        errors.append({"path": "channel", "reason": "invalid_channel"})

    first_seen = raw.get("first_seen_at")
    if _parse_iso(str(first_seen or "")) is None:
        errors.append({"path": "first_seen_at", "reason": "invalid_timestamp"})

    exchanges = raw.get("exchanges", 0)
    if exchanges is not None:
        if not isinstance(exchanges, int) or exchanges < 0:
            errors.append({"path": "exchanges", "reason": "invalid_exchanges"})

    for field in ("last_exchange_at", "answered_at", "unanswered_since"):
        if field in raw and raw[field] is not None:
            if _parse_iso(str(raw[field])) is None:
                errors.append({"path": field, "reason": "invalid_timestamp"})

    topics = raw.get("topics")
    if topics is not None:
        if not isinstance(topics, list):
            errors.append({"path": "topics", "reason": "topics_not_array"})
        elif len(topics) > 50:
            errors.append({"path": "topics", "reason": "too_many_topics"})
        else:
            for i, topic in enumerate(topics):
                if not topic or not str(topic).strip():
                    errors.append({"path": f"topics[{i}]", "reason": "empty_topic"})
                elif len(str(topic)) > 200:
                    errors.append({"path": f"topics[{i}]", "reason": "topic_too_long"})

    wig = raw.get("what_i_got")
    if wig is not None and len(str(wig)) > 8000:
        errors.append({"path": "what_i_got", "reason": "what_i_got_too_long"})

    endpoint = raw.get("endpoint")
    if endpoint is not None and len(str(endpoint)) > 2000:
        errors.append({"path": "endpoint", "reason": "endpoint_too_long"})

    allowed = set(json_schema()["properties"]) | {"v"}
    for key in raw:
        if key not in allowed:
            errors.append({"path": key, "reason": "unknown_field"})

    if errors:
        return None, errors

    normalized: dict[str, Any] = {
        "v": VERSION,
        "who": str(who).strip(),
        "channel": channel,
        "first_seen_at": str(first_seen).strip(),
    }
    if "exchanges" in raw:
        normalized["exchanges"] = int(exchanges)
    for optional in (
        "last_exchange_at",
        "topics",
        "what_i_got",
        "answered_at",
        "unanswered_since",
        "endpoint",
    ):
        if optional in raw and raw[optional] is not None:
            normalized[optional] = raw[optional]

    answered = normalized.get("answered_at")
    unanswered = normalized.get("unanswered_since")
    if answered and unanswered:
        a_dt = _parse_iso(answered)
        u_dt = _parse_iso(unanswered)
        if a_dt and u_dt and a_dt > u_dt:
            errors.append(
                {
                    "path": "answered_at",
                    "reason": "answered_after_unanswered_marker",
                }
            )
            return None, errors

    return normalized, []


def validation_payload(card: dict[str, Any]) -> dict[str, Any]:
    normalized, errors = validate_card(card)
    return {
        "valid": not errors,
        "v": VERSION,
        "errors": errors,
        "normalized": normalized,
    }


def store_payload(card: dict[str, Any]) -> dict[str, Any]:
    """Valide une carte et renvoie ce qu'un agent doit persister localement."""
    out = validation_payload(card)
    out["persist"] = "caller"
    out["hint"] = (
        "Le serveur est sans compte : après valid=true, écrire normalized "
        "dans votre store local (fichier, SQLite, graphe) et l'injecter "
        "avant chaque tour avec cet interlocuteur."
    )
    return out


def retrieve_payload(
    *,
    who: str | None = None,
    mcp_endpoint: str,
    http_base: str,
) -> dict[str, Any]:
    """Schéma, exemple et gabarit optionnel pour charger une carte locale."""
    base = http_base.rstrip("/")
    out: dict[str, Any] = {
        "v": VERSION,
        "schema_url": SCHEMA_ID,
        "schema": json_schema(),
        "sample": {**SAMPLE_CARD, "sample": True},
        "http": {
            "validate": f"{base}/relationship-memory/validate",
            "store": f"{base}/relationship-memory/store",
            "retrieve": f"{base}/relationship-memory/retrieve",
            "sample": f"{base}/relationship-memory/sample",
        },
        "mcp_endpoint": mcp_endpoint,
        "mcp_tools": [
            "relationship_memory.validate",
            "relationship_memory.store",
            "relationship_memory.retrieve",
        ],
        "guide": f"{base}/place/guide-relationship-memory-agents",
    }
    if who and str(who).strip():
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        out["starter_card"] = {
            "v": VERSION,
            "who": str(who).strip()[:500],
            "channel": "mcp",
            "first_seen_at": now,
            "exchanges": 0,
        }
    return out
