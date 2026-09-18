import asyncio
from unittest.mock import AsyncMock, patch

from starlette.requests import Request

from app.handlers.discover import discover_sample


def _sample_request() -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/discover/sample",
        "headers": [],
        "server": ("x402.agentindex.world", 443),
        "scheme": "https",
    }
    return Request(scope)


def test_discover_sample_includes_paid_upgrade() -> None:
    fake = {"q": "extract text from a PDF", "results": [{"name": "Example"}]}
    with patch(
        "app.handlers.discover._run_snapshot_discover",
        new=AsyncMock(return_value=fake),
    ):
        body = asyncio.run(discover_sample(_sample_request()))
    assert body["results"] == fake["results"]
    upgrade = body["paid_upgrade"]
    assert upgrade["method"] == "POST"
    assert upgrade["price_usdc"] == 0.001
    assert upgrade["url"] == "https://x402.agentindex.world/discover"
