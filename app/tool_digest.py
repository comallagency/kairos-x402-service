"""Digest canonique pour résultats d'outils MCP / tool_result.

Les agents comparent ce qu'ils ont reçu à ce qu'un pair a promis, sans
confiance aveugle. Algorithme stable : enveloppe JSON v1 + SHA-256.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

ALGORITHM = "sha256"
VERSION = 1
MAX_CONTENT_CHARS = 512_000


def _parse_content(content: Any) -> Any:
    if content is None:
        return None
    if isinstance(content, str):
        stripped = content.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            try:
                return json.loads(content)
            except json.JSONDecodeError:
                return content
        return content
    return content


def canonical_bytes(obj: Any) -> bytes:
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def build_envelope(
    tool_name: str,
    content: Any,
    *,
    tool_use_id: str | None = None,
) -> dict[str, Any]:
    if not tool_name or not str(tool_name).strip():
        raise ValueError("missing_tool_name")
    parsed = _parse_content(content)
    if isinstance(parsed, str) and len(parsed) > MAX_CONTENT_CHARS:
        raise ValueError("content_too_large")
    if parsed is not None and not isinstance(parsed, str):
        blob = canonical_bytes(parsed)
        if len(blob) > MAX_CONTENT_CHARS:
            raise ValueError("content_too_large")
    envelope: dict[str, Any] = {
        "v": VERSION,
        "tool_name": str(tool_name).strip(),
        "content": parsed,
    }
    if tool_use_id is not None and str(tool_use_id).strip():
        envelope["tool_use_id"] = str(tool_use_id).strip()
    return envelope


def digest_tool_result(
    tool_name: str,
    content: Any,
    *,
    tool_use_id: str | None = None,
) -> dict[str, Any]:
    envelope = build_envelope(tool_name, content, tool_use_id=tool_use_id)
    raw = canonical_bytes(envelope)
    return {
        "algorithm": ALGORITHM,
        "digest": hashlib.sha256(raw).hexdigest(),
        "v": VERSION,
        "tool_name": envelope["tool_name"],
        "tool_use_id": envelope.get("tool_use_id"),
        "content_bytes": len(raw),
    }


def verify_tool_result_digest(
    expected_digest: str,
    tool_name: str,
    content: Any,
    *,
    tool_use_id: str | None = None,
) -> dict[str, Any]:
    if not expected_digest or not str(expected_digest).strip():
        raise ValueError("missing_digest")
    computed = digest_tool_result(
        tool_name, content, tool_use_id=tool_use_id
    )
    exp = str(expected_digest).strip().lower()
    got = computed["digest"].lower()
    return {
        "match": exp == got,
        "algorithm": ALGORITHM,
        "expected_digest": exp,
        "computed_digest": got,
        "v": VERSION,
    }
