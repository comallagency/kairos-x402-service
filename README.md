# kairos-x402-service

[![Agent Economy Report rating](https://agenteconomy.report/s/x402.agentindex.world.svg)](https://agenteconomy.report/s/x402.agentindex.world) [![Agent Economy Report verified](https://agenteconomy.report/s/x402.agentindex.world.verified.svg)](https://agenteconomy.report/s/x402.agentindex.world)

Service HTTP et MCP **x402** sur **https://x402.agentindex.world** — **services payants uniquement**.

Routes payantes (USDC sur Base) : `search`, `pdf`, `web-read`, `extract`, `summarize`, `fact-check`, `translate`, `jobs`, `discover`.

Découverte technique : `GET /capabilities`, `/.well-known/x402`, `/.well-known/mcp.json`, samples `/*/sample`, `GET /health`.

## Déploiement

```bash
docker compose build && docker compose up -d
```
