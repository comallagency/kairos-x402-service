"""Refus honnête de livraison v1 — quand un vendeur ne peut pas livrer.

Les reçus de livraison couvrent le succès ; ce format couvre l'échec explicite
après engagement (402 accepté, paiement reçu, ou promesse d'outil). L'acheteur
peut journaliser un refus structuré au lieu d'un tool_result vide ou mensonger.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

VERSION = 1
SCHEMA_ID = "https://x402.agentindex.world/.well-known/honest-delivery-refusal.json"

REFUSAL_CODES = frozenset(
    {
        "upstream_unavailable",
        "invalid_request",
        "payment_required",
        "payment_not_sufficient",
        "capacity_limited",
        "policy_denied",
        "timeout",
        "partial_only",
        "other",
    }
)
REMEDY_TYPES = frozenset({"retry", "refund", "alternate_route", "contact", "none"})
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
        "title": "Honest delivery refusal",
        "description": (
            "Portable record when a seller cannot deliver after x402 engagement — "
            "structured reason and optional remedy for the buyer's journal."
        ),
        "type": "object",
        "required": ["v", "seller", "route", "refused_at", "code", "summary"],
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
            "refused_at": {"type": "string", "format": "date-time"},
            "code": {"type": "string", "enum": sorted(REFUSAL_CODES)},
            "summary": {"type": "string", "minLength": 1, "maxLength": 500},
            "detail": {"type": "string", "maxLength": 4000},
            "request_digest": {"type": "string", "pattern": "^[a-fA-F0-9]{64}$"},
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
            "remedy": {
                "type": "object",
                "required": ["type"],
                "additionalProperties": False,
                "properties": {
                    "type": {"type": "string", "enum": sorted(REMEDY_TYPES)},
                    "hint": {"type": "string", "maxLength": 2000},
                    "retry_after_seconds": {"type": "integer", "minimum": 0},
                    "alternate_route": {"type": "string", "maxLength": 500},
                    "contact_url": {"type": "string", "maxLength": 2000},
                },
            },
        },
    }


SAMPLE_REFUSAL: dict[str, Any] = {
    "v": VERSION,
    "seller": "https://x402.agentindex.world",
    "route": "/summarize",
    "refused_at": "2026-09-18T11:00:00+00:00",
    "code": "upstream_unavailable",
    "summary": "Upstream reader timed out after payment was accepted.",
    "detail": (
        "The paid summarize call could not fetch the target URL within 30s. "
        "No tool_result digest is emitted; buyer should keep this refusal."
    ),
    "payment": {
        "network": "base",
        "token": "USDC",
        "amount_usdc": 0.05,
        "from": "0x1111111111111111111111111111111111111111",
        "to": "0xb3F32bdfe8D07825BC0D7387295aB1D7559BA69d",
        "tx_ref": "sample-not-on-chain",
    },
    "remedy": {
        "type": "retry",
        "hint": "Retry with a reachable URL or use /web-read first.",
        "retry_after_seconds": 60,
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


def _check_addr(path: str, value: Any, errors: list[dict[str, str]]) -> None:
    if not value or not _HEX_ADDR.match(str(value).strip()):
        errors.append({"path": path, "reason": "invalid_address"})


def validate_refusal(raw: Any) -> tuple[dict[str, Any] | None, list[dict[str, str]]]:
    errors: list[dict[str, str]] = []

    if not isinstance(raw, dict):
        return None, [{"path": "", "reason": "not_an_object"}]

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

    if _parse_iso(str(raw.get("refused_at") or "")) is None:
        errors.append({"path": "refused_at", "reason": "invalid_timestamp"})

    code = raw.get("code")
    if code not in REFUSAL_CODES:
        errors.append({"path": "code", "reason": "invalid_code"})

    summary = raw.get("summary")
    if not summary or not str(summary).strip():
        errors.append({"path": "summary", "reason": "missing_summary"})
    elif len(str(summary)) > 500:
        errors.append({"path": "summary", "reason": "summary_too_long"})

    detail = raw.get("detail")
    if detail is not None and len(str(detail)) > 4000:
        errors.append({"path": "detail", "reason": "detail_too_long"})

    rd = raw.get("request_digest")
    if rd is not None and not _DIGEST.match(str(rd).strip()):
        errors.append({"path": "request_digest", "reason": "invalid_digest"})

    payment = raw.get("payment")
    if payment is not None:
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

    remedy = raw.get("remedy")
    if remedy is not None:
        if not isinstance(remedy, dict):
            errors.append({"path": "remedy", "reason": "remedy_not_object"})
        else:
            rtype = remedy.get("type")
            if rtype not in REMEDY_TYPES:
                errors.append({"path": "remedy.type", "reason": "invalid_remedy_type"})
            hint = remedy.get("hint")
            if hint is not None and len(str(hint)) > 2000:
                errors.append({"path": "remedy.hint", "reason": "hint_too_long"})
            ras = remedy.get("retry_after_seconds")
            if ras is not None and (not isinstance(ras, int) or ras < 0):
                errors.append(
                    {"path": "remedy.retry_after_seconds", "reason": "invalid_retry_after"}
                )
            alt = remedy.get("alternate_route")
            if alt is not None and len(str(alt)) > 500:
                errors.append({"path": "remedy.alternate_route", "reason": "route_too_long"})
            curl = remedy.get("contact_url")
            if curl is not None and len(str(curl)) > 2000:
                errors.append({"path": "remedy.contact_url", "reason": "contact_url_too_long"})

    allowed = set(json_schema()["properties"]) | {"v"}
    for key in raw:
        if key not in allowed:
            errors.append({"path": key, "reason": "unknown_field"})

    if errors:
        return None, errors

    normalized: dict[str, Any] = {
        "v": VERSION,
        "seller": str(seller).strip(),
        "route": str(route).strip(),
        "refused_at": str(raw["refused_at"]).strip(),
        "code": code,
        "summary": str(summary).strip(),
    }
    if detail:
        normalized["detail"] = str(detail).strip()
    if rd:
        normalized["request_digest"] = str(rd).strip().lower()
    if isinstance(payment, dict):
        normalized["payment"] = {
            "network": payment["network"],
            "token": str(payment["token"]).strip(),
            "amount_usdc": float(payment["amount_usdc"]),
            "from": str(payment["from"]).strip(),
            "to": str(payment["to"]).strip(),
        }
        if payment.get("tx_ref"):
            normalized["payment"]["tx_ref"] = str(payment["tx_ref"]).strip()
    if isinstance(remedy, dict):
        norm_remedy: dict[str, Any] = {"type": remedy["type"]}
        if remedy.get("hint"):
            norm_remedy["hint"] = str(remedy["hint"]).strip()
        if remedy.get("retry_after_seconds") is not None:
            norm_remedy["retry_after_seconds"] = int(remedy["retry_after_seconds"])
        if remedy.get("alternate_route"):
            norm_remedy["alternate_route"] = str(remedy["alternate_route"]).strip()
        if remedy.get("contact_url"):
            norm_remedy["contact_url"] = str(remedy["contact_url"]).strip()
        normalized["remedy"] = norm_remedy

    return normalized, []
