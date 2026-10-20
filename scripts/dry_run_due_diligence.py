"""One-off dry-run script (not a test, not committed to CI) - boots the real
FastAPI app in-process and exercises POST /due-diligence's wiring end to
end: router registration, OpenAPI schema, .well-known/x402 pricing, and the
MCP tool list - before any real deployment. Run with:
    uv run python scripts/dry_run_due_diligence.py
"""

import asyncio
import json

from fastapi.testclient import TestClient

from app.main import app


def main() -> None:
    with TestClient(app) as client:
        print("--- GET /due-diligence/sample (free) ---")
        r = client.get("/due-diligence/sample")
        print(r.status_code)
        body = r.json()
        print("keys:", sorted(body.keys()))
        assert body["token_risk"]["address"], "token_risk leg missing from combined response"
        assert body["wallet_intelligence"]["address"], "wallet_intelligence leg missing from combined response"
        assert body["agent_health"]["url"], "agent_health leg missing from combined response"
        print("overall_verdict:", body["overall_verdict"])

        print("\n--- GET /openapi.json: due-diligence entry ---")
        r = client.get("/openapi.json")
        spec = r.json()
        post_op = spec["paths"]["/due-diligence"]["post"]
        print("x-payment-info present:", "x-payment-info" in post_op)
        print("requestBody present:", "requestBody" in post_op)
        print("200 schema present:", bool(post_op["responses"]["200"]["content"]["application/json"]["schema"]))

        print("\n--- GET /.well-known/x402: due-diligence pricing ---")
        r = client.get("/.well-known/x402")
        resources = r.json()["resources"]
        matches = [res for res in resources if res["resource"].endswith("/due-diligence")]
        for m in matches:
            print(m["method"], m["resource"], "->", [a["price"] for a in m["accepts"]])

        print("\n--- MCP tool list: due_diligence present? ---")

    async def _list_mcp_tools():
        from app.mcp_server import mcp

        tools = await mcp.list_tools()
        return sorted(t.name for t in tools)

    tool_names = asyncio.run(_list_mcp_tools())
    print("due_diligence" in tool_names, "- total tools:", len(tool_names))


if __name__ == "__main__":
    main()
