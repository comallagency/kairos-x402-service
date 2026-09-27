# kairos-x402-service

Service HTTP et MCP **x402** sur **https://x402.agentindex.world** — **services payants uniquement**.

Routes payantes (USDC sur Base) : `search`, `pdf`, `web-read`, `extract`, `summarize`, `fact-check`, `translate`, `jobs`, `discover`.

Découverte technique : `GET /capabilities`, `/.well-known/x402`, `/.well-known/mcp.json`, samples `/*/sample`, `GET /health`.

## Add to Claude / Cursor

This server is remote (Streamable HTTP) and listed in the official MCP registry as
[`world.agentindex/x402`](https://registry.modelcontextprotocol.io/v0/servers?search=world.agentindex).
No API key or account needed — each tool call is paid per-use in USDC on Base (x402); your
client's wallet handles the payment. Add it to your MCP config:

```json
{
  "mcpServers": {
    "agentindex-x402": {
      "type": "http",
      "url": "https://x402.agentindex.world/mcp/"
    }
  }
}
```

- **Claude Code**: add the block above to `.mcp.json` in your project root, or run
  `claude mcp add --transport http agentindex-x402 https://x402.agentindex.world/mcp/`.
- **Claude Desktop**: Settings → Connectors → Add custom connector, with the same URL.
- **Cursor**: add the block above to `.cursor/mcp.json` (project) or `~/.cursor/mcp.json`
  (global) — the `type` field is optional there.

## Déploiement

```bash
docker compose build && docker compose up -d
```
