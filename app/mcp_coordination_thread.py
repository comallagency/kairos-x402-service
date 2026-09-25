"""Serveur MCP dédié — fil de coordination (gratuit, sans compte).

Monté sur /mcp/coordination-thread/ pour la découverte et les clients MCP
qui veulent valider des tours multi-agents sans charger le bundle /mcp/ payant.
"""

from fastmcp import FastMCP

from app import config
from app.coordination_thread import retrieve_payload, validation_payload

_BASE = config.BASE_URL.rstrip("/")
_MCP_ENDPOINT = f"{_BASE}/mcp/coordination-thread/"

mcp = FastMCP(
    "Kairos Coordination Thread",
    instructions=(
        "Free MCP surface for portable multi-agent coordination thread turns (v1). "
        "Complements relationship memory (who): each turn has thread_id, speaker, "
        "channel, timestamp, message, optional in_reply_to and trust-kit artifact refs. "
        "The server does not hold your thread — coordination_thread.validate checks "
        "a turn; coordination_thread.retrieve returns schema, sample and a starter turn. "
        f"HTTP mirror: {_BASE}/coordination-thread/. "
        f"Schema: {_BASE}/.well-known/coordination-thread-turn.json"
    ),
)


@mcp.tool(
    name="coordination_thread.validate",
    description=(
        "Validate a coordination thread turn against the v1 JSON Schema — free. "
        "Pass the full turn object."
    ),
)
async def validate_tool(turn: dict) -> dict:
    return validation_payload(turn)


@mcp.tool(
    name="coordination_thread.retrieve",
    description=(
        "Return schema, sample, HTTP and MCP entrypoints. "
        "Optional speaker= and thread_id= pre-fill a starter turn."
    ),
)
async def retrieve_tool(
    speaker: str | None = None,
    thread_id: str | None = None,
) -> dict:
    return retrieve_payload(
        speaker=speaker,
        thread_id=thread_id,
        mcp_endpoint=_MCP_ENDPOINT,
        http_base=_BASE,
    )
