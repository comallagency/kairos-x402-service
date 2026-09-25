"""Serveur MCP dédié — mémoire de relation (gratuit, sans compte).

Monté sur /mcp/relationship-memory/ pour la découverte et la métrique chantier
(tools/call sur relationship_memory.store | .retrieve | .validate).
"""

from fastmcp import FastMCP

from app import config
from app.relationship_memory import (
    retrieve_payload,
    store_payload,
    validation_payload,
)

_BASE = config.BASE_URL.rstrip("/")
_MCP_ENDPOINT = f"{_BASE}/mcp/relationship-memory/"

mcp = FastMCP(
    "Kairos Relationship Memory",
    instructions=(
        "Free MCP surface for portable agent relationship memory cards (v1). "
        "Remember interlocutors, not isolated messages: who, channel, topics, "
        "what you learned, unanswered outreach. "
        "The server does not hold your memory — relationship_memory.store validates "
        "and returns a normalized card for you to persist locally; "
        "relationship_memory.retrieve returns schema, sample and optional starter "
        "card; relationship_memory.validate checks a card only. "
        f"HTTP mirror: {_BASE}/relationship-memory/. "
        f"Schema: {_BASE}/.well-known/relationship-memory.json"
    ),
)


@mcp.tool(
    name="relationship_memory.validate",
    description=(
        "Validate a relationship memory card against the v1 JSON Schema — free. "
        "Pass the full card object."
    ),
)
async def validate_tool(card: dict) -> dict:
    return validation_payload(card)


@mcp.tool(
    name="relationship_memory.store",
    description=(
        "Validate then return a normalized card ready to persist on the caller side — "
        "free, stateless (no server-side database)."
    ),
)
async def store_tool(card: dict) -> dict:
    return store_payload(card)


@mcp.tool(
    name="relationship_memory.retrieve",
    description=(
        "Return schema, sample, HTTP and MCP entrypoints. "
        "Optional who= pre-fills a starter card for a new interlocutor."
    ),
)
async def retrieve_tool(who: str | None = None) -> dict:
    return retrieve_payload(
        who=who,
        mcp_endpoint=_MCP_ENDPOINT,
        http_base=_BASE,
    )
