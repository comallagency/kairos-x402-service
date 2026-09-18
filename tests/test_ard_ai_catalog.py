"""Manifestes ARD (ai-catalog.json et ard.json)."""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_ai_catalog_and_ard_same_manifest() -> None:
    catalog = client.get("/.well-known/ai-catalog.json")
    ard = client.get("/.well-known/ard.json")
    assert catalog.status_code == 200
    assert ard.status_code == 200
    assert catalog.json() == ard.json()


def test_ai_catalog_lists_mcp_and_discover() -> None:
    body = client.get("/.well-known/ai-catalog.json").json()
    assert body["specVersion"] == "1.0"
    assert body["host"]["identifier"] == "x402.agentindex.world"
    ids = {e["identifier"] for e in body["entries"]}
    assert "urn:air:x402.agentindex.world:mcp:agentindex-x402" in ids
    assert "urn:air:x402.agentindex.world:mcp:relationship-memory" in ids
    assert "urn:air:x402.agentindex.world:discover:free" in ids
