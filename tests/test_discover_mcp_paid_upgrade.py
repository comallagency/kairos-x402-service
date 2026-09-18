import asyncio
from unittest.mock import AsyncMock, patch

from app.mcp_server import discover_mcp_servers_tool


def test_discover_mcp_servers_includes_paid_upgrade() -> None:
    fake = {"q": "jdbc read only", "results": [{"name": "JDBC MCP Server"}]}
    with patch(
        "app.mcp_server._run_discover_snapshot",
        new=AsyncMock(return_value=fake),
    ), patch("app.mcp_server.db.log_request"):
        body = asyncio.run(discover_mcp_servers_tool("jdbc read only", 5))
    assert body["results"] == fake["results"]
    upgrade = body["paid_upgrade"]
    assert upgrade["method"] == "POST"
    assert upgrade["price_usdc"] == 0.001
    assert upgrade["mcp_tool"] == "discover_semantic"
    assert upgrade["url"].endswith("/discover")
