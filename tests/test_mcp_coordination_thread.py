"""Serveur MCP dédié coordination-thread."""

import asyncio
import json

from fastapi.testclient import TestClient

from app.coordination_thread import SAMPLE_TURN
from app.main import app
from app.mcp_coordination_thread import retrieve_tool, validate_tool

client = TestClient(app)


def test_http_retrieve():
    r = client.get("/coordination-thread/retrieve", params={"speaker": "peer-ct"})
    assert r.status_code == 200
    data = r.json()
    assert "coordination_thread.validate" in data["mcp_tools"]
    assert data["starter_turn"]["speaker"] == "peer-ct"


def test_well_known_mcp_coordination_thread_card():
    r = client.get("/.well-known/mcp/coordination-thread.json")
    assert r.status_code == 200
    assert "mcp/coordination-thread" in r.json()["mcpEndpoint"]


def test_mcp_tools_unit():
    turn = {k: v for k, v in SAMPLE_TURN.items()}
    v = asyncio.run(validate_tool(turn))
    assert v["valid"] is True
    ret = asyncio.run(retrieve_tool(speaker="unit"))
    assert ret["starter_turn"]["speaker"] == "unit"


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
        "/mcp/coordination-thread/",
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
        "/mcp/coordination-thread/",
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
        "coordination_thread.validate",
        "coordination_thread.retrieve",
    }
