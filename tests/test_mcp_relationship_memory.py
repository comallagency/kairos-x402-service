"""Serveur MCP dédié relationship-memory."""

import asyncio
import json

from fastapi.testclient import TestClient

from app.main import app
from app.mcp_relationship_memory import (
    retrieve_tool,
    store_tool,
    validate_tool,
)

client = TestClient(app)


def test_http_store_and_retrieve():
    body = {
        "card": {
            "v": 1,
            "who": "peer-mcp",
            "channel": "mcp",
            "first_seen_at": "2026-09-18T18:00:00+00:00",
            "exchanges": 1,
        }
    }
    r = client.post("/relationship-memory/store", json=body)
    assert r.status_code == 200
    data = r.json()
    assert data["valid"] is True
    assert data["persist"] == "caller"

    r2 = client.get("/relationship-memory/retrieve", params={"who": "peer-mcp"})
    assert r2.status_code == 200
    card = r2.json()
    assert "relationship_memory.validate" in card["mcp_tools"]
    assert card["starter_card"]["who"] == "peer-mcp"


def test_well_known_mcp_relationship_memory_card():
    r = client.get("/.well-known/mcp/relationship-memory.json")
    assert r.status_code == 200
    assert "mcp/relationship-memory" in r.json()["mcpEndpoint"]


def test_mcp_tools_unit():
    card = {
        "v": 1,
        "who": "unit",
        "channel": "http",
        "first_seen_at": "2026-09-18T18:00:00+00:00",
    }
    v = asyncio.run(validate_tool(card))
    assert v["valid"] is True
    s = asyncio.run(store_tool(card))
    assert s["persist"] == "caller"
    ret = asyncio.run(retrieve_tool(who="unit"))
    assert ret["starter_card"]["who"] == "unit"


def test_mcp_initialize_and_tools_list():
    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "1.0"},
        },
    }
    r = client.post(
        "/mcp/relationship-memory/",
        json=init,
        headers={
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        },
    )
    assert r.status_code == 200
    session = r.headers.get("mcp-session-id") or r.headers.get("Mcp-Session-Id")
    assert session

    tools_req = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
    r2 = client.post(
        "/mcp/relationship-memory/",
        json=tools_req,
        headers={
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "Mcp-Session-Id": session,
        },
    )
    assert r2.status_code == 200
    raw = r2.text
    if raw.strip().startswith("event:"):
        for line in raw.split("\n"):
            if line.startswith("data:"):
                payload = json.loads(line[5:].strip())
                break
    else:
        payload = r2.json()
    names = {t["name"] for t in payload.get("result", {}).get("tools", [])}
    assert names == {
        "relationship_memory.validate",
        "relationship_memory.store",
        "relationship_memory.retrieve",
    }
