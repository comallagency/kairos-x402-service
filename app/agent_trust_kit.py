"""Manifeste agent-trust-kit v1 — index machine des formats gratuits Kairos.

Un agent tiers peut GET /.well-known/agent-trust-kit.json une fois et obtenir
schémas, validateurs, outils MCP et parcours suggéré (acheteur x402, vendeur,
coordination multi-agents) sans parcourir agent.json à la main.
"""

from __future__ import annotations

from typing import Any

from app import config

VERSION = 1
SCHEMA_ID = "https://x402.agentindex.world/.well-known/agent-trust-kit.json"


def _url(path: str) -> str:
    base = config.BASE_URL.rstrip("/")
    if not path.startswith("/"):
        path = "/" + path
    return base + path


def manifest() -> dict[str, Any]:
    base = config.BASE_URL.rstrip("/")
    formats: list[dict[str, Any]] = [
        {
            "id": "relationship-memory",
            "role": "who",
            "title": "Relationship memory card",
            "schema": _url("/.well-known/relationship-memory.json"),
            "validate": {"method": "POST", "url": _url("/relationship-memory/validate")},
            "sample": _url("/relationship-memory/sample"),
            "mcp_tools": ["relationship_memory_schema", "validate_relationship_memory"],
            "guide": "https://comallagency.github.io/kairos-place/relationship-memory.html",
        },
        {
            "id": "tool-result-digest",
            "role": "integrity",
            "title": "Canonical tool_result digest",
            "schema": _url("/.well-known/tool-result-digest.json"),
            "validate": {"method": "POST", "url": _url("/tool-result-verify")},
            "sample": _url("/tool-result-digest/sample"),
            "mcp_tools": ["tool_result_digest_schema", "verify_tool_result_digest"],
            "guide": "https://comallagency.github.io/kairos-place/tool-result-digest.html",
        },
        {
            "id": "tool-delivery-receipt",
            "role": "success",
            "title": "Tool delivery receipt (payment + digest)",
            "schema": _url("/.well-known/tool-delivery-receipt.json"),
            "validate": {"method": "POST", "url": _url("/tool-delivery-receipt/validate")},
            "sample": _url("/tool-delivery-receipt/sample"),
            "mcp_tools": ["tool_delivery_receipt_schema", "validate_tool_delivery_receipt"],
            "guide": "https://comallagency.github.io/kairos-place/tool-delivery-receipt.html",
        },
        {
            "id": "honest-delivery-refusal",
            "role": "failure",
            "title": "Honest delivery refusal",
            "schema": _url("/.well-known/honest-delivery-refusal.json"),
            "validate": {"method": "POST", "url": _url("/honest-delivery-refusal/validate")},
            "sample": _url("/honest-delivery-refusal/sample"),
            "mcp_tools": ["honest_delivery_refusal_schema", "validate_honest_delivery_refusal"],
            "guide": "https://comallagency.github.io/kairos-place/honest-delivery-refusal.html",
        },
        {
            "id": "coordination-thread-turn",
            "role": "thread",
            "title": "Coordination thread turn",
            "schema": _url("/.well-known/coordination-thread-turn.json"),
            "validate": {"method": "POST", "url": _url("/coordination-thread/validate")},
            "sample": _url("/coordination-thread/sample"),
            "mcp_tools": ["coordination_thread_schema", "validate_coordination_thread_turn"],
            "guide": "https://comallagency.github.io/kairos-place/coordination-thread.html",
        },
        {
            "id": "return-visit-pledge",
            "role": "return",
            "title": "Return visit pledge",
            "schema": _url("/.well-known/return-visit-pledge.json"),
            "validate": {"method": "POST", "url": _url("/return-visit-pledge/validate")},
            "sample": _url("/return-visit-pledge/sample"),
            "mcp_tools": ["return_visit_pledge_schema", "validate_return_visit_pledge"],
            "guide": "https://comallagency.github.io/kairos-place/return-visit-pledge.html",
        },
        {
            "id": "coordination-thread-snapshot",
            "role": "handoff",
            "title": "Coordination thread snapshot",
            "schema": _url("/.well-known/coordination-thread-snapshot.json"),
            "validate": {
                "method": "POST",
                "url": _url("/coordination-thread-snapshot/validate"),
            },
            "sample": _url("/coordination-thread-snapshot/sample"),
            "mcp_tools": [
                "coordination_thread_snapshot_schema",
                "validate_coordination_thread_snapshot",
            ],
            "guide": "https://comallagency.github.io/kairos-place/coordination-thread-snapshot.html",
        },
    ]

    return {
        "v": VERSION,
        "$id": SCHEMA_ID,
        "issuer": base,
        "description": (
            "Free portable formats for agent trust and coordination around x402 — "
            "schemas and validators only, no storage."
        ),
        "manifest": _url("/.well-known/agent-trust-kit.json"),
        "human_guide": _url("/place/agent-trust-kit"),
        "mcp_endpoint": _url("/mcp"),
        "cli": {
            "repository": "https://github.com/comallagency/kairos",
            "path": "outils/agent_trust_kit_cli.py",
            "env_base": "KAIROS_TRUST_BASE",
        },
        "formats": formats,
        "workflow": {
            "buyer_x402": [
                {"step": 1, "format": "relationship-memory", "action": "Record who sells."},
                {
                    "step": 2,
                    "format": "tool-result-digest",
                    "action": "Hash or verify the delivered tool_result.",
                },
                {"step": 3, "action": "Pay the x402 route (402 → signed payment → 200)."},
                {
                    "step": 4,
                    "format": "tool-delivery-receipt",
                    "action": "On success, store a receipt linking payment to digest.",
                    "alternate_format": "honest-delivery-refusal",
                    "alternate_action": "On failure, store a structured refusal.",
                },
                {
                    "step": 5,
                    "format": "relationship-memory",
                    "action": "Update the interlocutor card with outcome.",
                },
            ],
            "coordination": [
                {"step": 1, "format": "coordination-thread-turn", "action": "Open or append a thread turn."},
                {
                    "step": 2,
                    "format": "return-visit-pledge",
                    "action": "Before leaving, pledge when you will return.",
                },
                {
                    "step": 3,
                    "format": "relationship-memory",
                    "action": "Refresh the peer card when the thread moves.",
                },
                {
                    "step": 4,
                    "format": "coordination-thread-snapshot",
                    "action": "Before shutdown, validate and write a snapshot file for the next run.",
                },
            ],
        },
    }
