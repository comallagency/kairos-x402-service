"""Reçu de livraison d'outil v1 — lier un paiement x402 à un digest tool_result.

Les cycles tool_call → tool_result fonctionnent sur l'honneur : ce format
permet à un agent acheteur de garder une preuve portable (qui, quelle route,
combien, digest SHA-256 du livrable) sans confiance aveugle.
Complète relationship-memory (qui) et tool-result-digest (hash canonique).
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from app.tool_digest import ALGORITHM, verify_tool_result_digest

VERSION = 1
SCHEMA_ID = "https://x402.agentindex.world/.well-known/tool-delivery-receipt.json"

_NETWORKS = frozenset({"base", "base-sepolia", "other"})
_HEX_ADDR = re.compile(r"^0x[a-fA-F0-9]{40}$")
_DIGEST = re.compile(r"^[a-fA-F0-9]{64}$")
_ISO_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)


def json_schema() -> dict[str, Any]:
    schema_key = "$" + "schema"
    return {
        schema_key: "https://json-schema.org/draft/2020-12/schema",
        "$id": SCHEMA_ID,
        "title": "Agent tool delivery receipt",
        "description": (
            "Portable record linking an x402 payment to a delivered tool_result "
            "digest — for buyers who must verify before trusting."
        ),
        "type": "object",
        "required": ["v", "seller", "route", "paid_at", "payment", "delivery"],
        "additionalProperties": False,
        "properties": {
            "v": {"type": "integer", "const": VERSION},
            "seller": {"type": "string", "minLength": 8, "maxLength": 2000},
            "route": {
                "type": "string",
                "pattern": "^/",
                "minLength": 1,
                "maxLength": 500,
            },
            "paid_at": {"type": "string", "format": "date-time"},
            "payment": {
                "type": "object",
                "required": ["network", "token", "amount_usdc", "from", "to"],
                "additionalProperties": False,
                "properties": {
                    "network": {"type": "string", "enum": sorted(_NETWORKS)},
                    "token": {"type": "string", "minLength": 1, "maxLength": 32},
                    "amount_usdc": {"type": "number", "minimum": 0},
                    "from": {"type": "string", "pattern": "^0x[a-fA-F0-9]{40}$"},
                    "to": {"type": "string", "pattern": "^0x[a-fA-F0-9]{40}$"},
                    "tx_ref": {"type": "string", "maxLength": 200},
                },
            },
            "delivery": {
                "type": "object",
                "required": ["tool_name", "algorithm", "digest"],
                "additionalProperties": False,
                "properties": {
                    "tool_name": {"type": "string", "minLength": 1, "maxLength": 200},
                    "algorithm": {"type": "string", "const": ALGORITHM},
                    "digest": {"type": "string", "pattern": "^[a-fA-F0-9]{64}$"},
                    "tool_use_id": {"type": "string", "maxLength": 200},
                },
            },
            "note": {"type": "string", "maxLength": 2000},
        },
    }


SAMPLE_RECEIPT: dict[str, Any] = {
    "v": VERSION,
    "seller": "https://x402.agentindex.world",
    "route": "/summarize",
    "paid_at": "2026-09-18T10:00:00+00:00",
    "payment": {
        "network": "base",
        "token": "USDC",
        "amount_usdc": 0.05,
        "from": "0x1111111111111111111111111111111111111111",
        "to": "0xb3F32bdfe8D07825BC0D7387295aB1D7559BA69d",
        "tx_ref": "sample-not-on-chain",
    },
    "delivery": {
        "tool_name": "summarize_text",
        "algorithm": "sha256",
        "digest": "1c3d51071ea068435eb28032563a81df18547b4bd2e067c1f0d3ba2e3e0b7161",
        "tool_use_id": "tu_sample_01",
    },
    "note": "Digest matches GET /tool-result-digest/sample fixture.",
}


def _parse_iso(value: str) -> datetime | None:
    if not value or not _ISO_RE.match(value.strip()):
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _check_addr(path: str, value: Any, errors: list[dict[str, str]]) -> None:
    if not value or not _HEX_ADDR.match(str(value).strip()):
        errors.append({"path": path, "reason": "invalid_address"})


def validate_receipt(
    raw: Any,
    *,
    content: Any = None,
) -> tuple[dict[str, Any] | None, list[dict[str, str]], dict[str, Any] | None]:
    errors: list[dict[str, str]] = []
    digest_check: dict[str, Any] | None = None

    if not isinstance(raw, dict):
        return None, [{"path": "", "reason": "not_an_object"}], None

    if raw.get("v") != VERSION:
        errors.append({"path": "v", "reason": "unsupported_version"})

    seller = raw.get("seller")
    if not seller or not str(seller).strip():
        errors.append({"path": "seller", "reason": "missing_seller"})
    elif len(str(seller)) > 2000:
        errors.append({"path": "seller", "reason": "seller_too_long"})

    route = raw.get("route")
    if not route or not str(route).startswith("/"):
        errors.append({"path": "route", "reason": "invalid_route"})
    elif len(str(route)) > 500:
        errors.append({"path": "route", "reason": "route_too_long"})

    if _parse_iso(str(raw.get("paid_at") or "")) is None:
        errors.append({"path": "paid_at", "reason": "invalid_timestamp"})

    payment = raw.get("payment")
    if not isinstance(payment, dict):
        errors.append({"path": "payment", "reason": "payment_not_object"})
    else:
        net = payment.get("network")
        if net not in _NETWORKS:
            errors.append({"path": "payment.network", "reason": "invalid_network"})
        token = payment.get("token")
        if not token or not str(token).strip():
            errors.append({"path": "payment.token", "reason": "missing_token"})
        amount = payment.get("amount_usdc")
        if not isinstance(amount, (int, float)) or amount < 0:
            errors.append({"path": "payment.amount_usdc", "reason": "invalid_amount"})
        _check_addr("payment.from", payment.get("from"), errors)
        _check_addr("payment.to", payment.get("to"), errors)
        tx_ref = payment.get("tx_ref")
        if tx_ref is not None and len(str(tx_ref)) > 200:
            errors.append({"path": "payment.tx_ref", "reason": "tx_ref_too_long"})

    delivery = raw.get("delivery")
    if not isinstance(delivery, dict):
        errors.append({"path": "delivery", "reason": "delivery_not_object"})
    else:
        tool_name = delivery.get("tool_name")
        if not tool_name or not str(tool_name).strip():
            errors.append({"path": "delivery.tool_name", "reason": "missing_tool_name"})
        if delivery.get("algorithm") != ALGORITHM:
            errors.append({"path": "delivery.algorithm", "reason": "unsupported_algorithm"})
        digest = delivery.get("digest")
        if not digest or not _DIGEST.match(str(digest).strip()):
            errors.append({"path": "delivery.digest", "reason": "invalid_digest"})
        tuid = delivery.get("tool_use_id")
        if tuid is not None and len(str(tuid)) > 200:
            errors.append({"path": "delivery.tool_use_id", "reason": "tool_use_id_too_long"})

    note = raw.get("note")
    if note is not None and len(str(note)) > 2000:
        errors.append({"path": "note", "reason": "note_too_long"})

    allowed = set(json_schema()["properties"]) | {"v"}
    for key in raw:
        if key not in allowed:
            errors.append({"path": key, "reason": "unknown_field"})

    if errors:
        return None, errors, digest_check

    normalized: dict[str, Any] = {
        "v": VERSION,
        "seller": str(seller).strip(),
        "route": str(route).strip(),
        "paid_at": str(raw["paid_at"]).strip(),
        "payment": {
            "network": payment["network"],
            "token": str(payment["token"]).strip(),
            "amount_usdc": float(payment["amount_usdc"]),
            "from": str(payment["from"]).strip(),
            "to": str(payment["to"]).strip(),
        },
        "delivery": {
            "tool_name": str(delivery["tool_name"]).strip(),
            "algorithm": ALGORITHM,
            "digest": str(delivery["digest"]).strip().lower(),
        },
    }
    if payment.get("tx_ref"):
        normalized["payment"]["tx_ref"] = str(payment["tx_ref"]).strip()
    if delivery.get("tool_use_id"):
        normalized["delivery"]["tool_use_id"] = str(delivery["tool_use_id"]).strip()
    if note:
        normalized["note"] = str(note).strip()

    if content is not None and normalized.get("delivery"):
        d = normalized["delivery"]
        try:
            digest_check = verify_tool_result_digest(
                d["digest"],
                d["tool_name"],
                content,
                tool_use_id=d.get("tool_use_id"),
            )
        except ValueError as exc:
            digest_check = {"match": False, "error": str(exc)}

    return normalized, [], digest_check
