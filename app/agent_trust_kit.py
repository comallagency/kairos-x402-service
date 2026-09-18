"""Kit de confiance agent v1 — index des formats gratuits pour paiements x402 honnêtes.

Les quatre formats (mémoire de relation, digest, reçu, refus) existent déjà ;
ce manifeste les relie en un parcours que d'autres agents peuvent découvrir
via un seul well-known ou un CLI local sans parser les logs Kairos.
"""

from __future__ import annotations

from typing import Any

from app import config

VERSION = 1
SCHEMA_ID = "https://x402.agentindex.world/.well-known/agent-trust-kit.json"


def _format_entry(
    base: str,
    *,
    id: str,
    title: str,
    role: str,
    schema_path: str,
    validate_path: str,
    sample_path: str,
) -> dict[str, Any]:
    return {
        "id": id,
        "title": title,
        "role": role,
        "schema": f"{base}{schema_path}",
        "validate": f"{base}{validate_path}",
        "sample": f"{base}{sample_path}",
    }


def build_manifest(base_url: str | None = None) -> dict[str, Any]:
    base = (base_url or config.BASE_URL).rstrip("/")
    formats = [
        _format_entry(
            base,
            id="relationship-memory",
            title="Relationship memory card",
            role="remember_who",
            schema_path="/.well-known/relationship-memory.json",
            validate_path="/relationship-memory/validate",
            sample_path="/relationship-memory/sample",
        ),
        _format_entry(
            base,
            id="tool-result-digest",
            title="Tool result digest",
            role="hash_deliverable",
            schema_path="/tool-result-digest/sample",
            validate_path="/tool-result-verify",
            sample_path="/tool-result-digest/sample",
        ),
        _format_entry(
            base,
            id="tool-delivery-receipt",
            title="Tool delivery receipt",
            role="record_success",
            schema_path="/.well-known/tool-delivery-receipt.json",
            validate_path="/tool-delivery-receipt/validate",
            sample_path="/tool-delivery-receipt/sample",
        ),
        _format_entry(
            base,
            id="honest-delivery-refusal",
            title="Honest delivery refusal",
            role="record_failure",
            schema_path="/.well-known/honest-delivery-refusal.json",
            validate_path="/honest-delivery-refusal/validate",
            sample_path="/honest-delivery-refusal/sample",
        ),
    ]
    by_id = {f["id"]: f for f in formats}
    return {
        "v": VERSION,
        "$id": SCHEMA_ID,
        "title": "Agent trust kit",
        "description": (
            "Free, stateless validators for agent coordination around x402: "
            "who you talked to, what hash you expected, what you paid for, "
            "or why delivery failed. No account, no storage."
        ),
        "publisher": "https://x402.agentindex.world",
        "human_guide": f"{base}/place/agent-trust-kit",
        "remote_mcp": f"{base}/mcp",
        "local_cli": {
            "repository": "https://github.com/comallagency/kairos",
            "path": "outils/agent_trust_kit_cli.py",
            "usage": "python3 outils/agent_trust_kit_cli.py validate <kind> --file payload.json",
        },
        "workflow": [
            {
                "step": 1,
                "action": "remember_interlocutor",
                "format": by_id["relationship-memory"],
            },
            {
                "step": 2,
                "action": "compute_or_verify_digest",
                "format": by_id["tool-result-digest"],
                "note": "POST /tool-result-digest to hash; POST /tool-result-verify to check.",
            },
            {
                "step": 3,
                "action": "pay_x402_route",
                "note": "402 → PAYMENT-SIGNATURE → 200 with tool_result content.",
            },
            {
                "step": 4,
                "action": "journal_outcome",
                "branches": [
                    {"when": "delivered", "format": by_id["tool-delivery-receipt"]},
                    {"when": "refused", "format": by_id["honest-delivery-refusal"]},
                ],
            },
            {
                "step": 5,
                "action": "update_relationship_memory",
                "format": by_id["relationship-memory"],
            },
        ],
        "formats": formats,
    }
