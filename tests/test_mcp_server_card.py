"""Carte MCP well-known : même forme capabilities que la carte agent A2A."""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_mcp_server_card_capabilities_object_and_paid_discover() -> None:
    r = client.get("/.well-known/mcp/server-card.json")
    assert r.status_code == 200
    card = r.json()
    alt = client.get("/.well-known/mcp.json")
    assert alt.status_code == 200
    assert alt.json() == card
    caps = card["capabilities"]
    assert isinstance(caps, dict)
    assert caps.get("streaming") is False
    assert card["capabilitiesUrl"].endswith("/capabilities")
    paid = card["discoverPaid"]
    assert paid["mcp_tool"] == "discover_semantic"
    assert paid["price_usdc"] == 0.001
    assert paid["url"].rstrip("/").endswith("/discover")
