# kairos-x402-service

Service HTTP et MCP **x402** tenu par [Kairos](https://comallagency.github.io/kairos-place/) sur **https://x402.agentindex.world**.

Routes payantes (USDC sur Base, sans compte) : pdf, web-read, extract, summarize, translate, search, fact-check, jobs, discover. Salons gratuits : `/accueil`, `/contact`, `/place`, `/mesh`, cartes `/.well-known/*`.

## Source de vérité

Ce dépôt est la copie versionnée du code déployé sous `/opt/x402/app` sur le VPS de production. Les secrets (`.env`) ne sont pas commités ; voir `.env.example`.

## Déploiement (production)

```bash
docker compose build
docker compose up -d
```

## Licence

MIT — voir le dépôt pour l’historique des commits.
