"""Outils MCP gratuits : salon d'accueil et contact (unitaires, sans session MCP)."""

import asyncio

from app.handlers.accueil import _payload
from app.handlers.contact import ContactIn, Declaration, deposer_contact, lire_contact
from app.mcp_server import (
    contact_kairos_tool,
    get_welcome_salon_tool,
    poll_contact_kairos_tool,
)


def test_get_welcome_salon_lists_mcp_tools():
    data = asyncio.run(get_welcome_salon_tool())
    assert data.get("who", {}).get("name") == "Kairos"
    tools = data.get("how_to_talk", {}).get("mcp_free_tools", [])
    assert "contact_kairos" in tools
    assert "get_welcome_salon" in tools


def test_accueil_payload_matches_tool():
    assert asyncio.run(get_welcome_salon_tool()) == _payload()


def test_contact_kairos_and_poll_roundtrip():
    receipt = asyncio.run(
        contact_kairos_tool(
            sender="pytest-agent/1.0",
            subject="MCP front door probe",
            body="Testing contact_kairos from pytest.",
            declares_what_i_do="Runs unit tests",
            declares_endpoint="https://example.com/agent",
            declares_skills=["testing"],
        )
    )
    assert receipt.get("id")
    mid = receipt["id"]
    poll = asyncio.run(poll_contact_kairos_tool(mid))
    assert poll["id"] == mid
    assert poll["declares"]["what_i_do"] == "Runs unit tests"


def test_deposer_contact_dedupe():
    payload = ContactIn(
        sender="dedupe/1",
        subject="same",
        body="same body",
    )
    a, c1 = deposer_contact(payload, user_agent="t", from_ip="1.2.3.4")
    b, c2 = deposer_contact(payload, user_agent="t", from_ip="1.2.3.4")
    assert c1 == 201
    assert c2 == 200
    assert a["id"] == b["id"]
    assert b.get("duplicate") is True
