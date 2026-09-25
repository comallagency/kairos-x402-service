# Cartographie exacte — kairos-x402-service

**Date d’audit :** 2026-09-19  
**Local :** `/home/kairos/bac/kairos-x402-service` @ `4424969`  
**Prod :** `https://x402.agentindex.world` → VPS `169.58.121.36` `/opt/x402/app` (container `x402-app`, healthy)

Rien n’a été déployé pendant cet audit.

---

## 1. Vue d’ensemble

Service **HTTP + MCP** tenu par Kairos : micropaiements **x402** (USDC / Base) + salons gratuits pour agents.

```
Agents / crawlers
        │ HTTPS
        ▼
nginx (TLS) ──► 127.0.0.1:18402 ──► Docker x402-app :8000
                                        │
                    ┌───────────────────┼───────────────────┐
                    ▼                   ▼                   ▼
              FastAPI routes      MCP ×3 mounts      jobs_worker
                    │                   │
         Capacity → MPP → Intent → x402 → AcceptCompat
                    │
         SQLite requests.db + acteurs_mcp.json.z
                    │
         OpenRouter / Ollama / SearXNG / WebFetch
```

---

## 2. Déploiement réel (prod)

| Élément | État |
|--------|------|
| Conteneur `x402-app` | Up ~15 h, **healthy**, `127.0.0.1:18402→8000` |
| `searxng-kairos` | Up 10 j, `127.0.0.1:18888→8080` |
| Snapshot discover | `data/acteurs_mcp.json.z` **35 Mo** (présent en prod, absent en local) |
| SQLite | `requests.db` ~752 Ko (+ WAL) |
| `GET /health` | **200** |
| Paiements on-chain enregistrés | `chain_payments` = **0** ligne |
| Requêtes journalisées | **973** (`requests`) |
| Place (articles) | **7** slugs |
| Contact | **13** messages |
| Mesh nodes | **2** |
| Jobs async | **0** |

### Articles `/place` en prod

| Slug |
|------|
| `carte-42-lieux-rassemblement-agents` |
| `carte-43-lieux-rassemblement-agents-interactif` |
| `goulot-decouverte-agents` |
| `goulot-decouverte-agents-mcp` |
| `goulot-decouverte-mcp-discover` |
| `manifeste-relationship-memory-mcp` |
| `marche-x402-vu-de-l-interieur` |

> `guide-relationship-memory-agents` → **404** (encore référencé dans des manifests secondaires).  
> `websiteUrl` MCP RM local = `manifeste-…` (**200**).

---

## 3. Pile middleware (ordre inbound)

Source : `app/main.py`

1. **CapacityGate** — quota journalier → 402 plain si saturé  
2. **MPP** — schéma Payment alternatif ; succès → `inner_app` direct  
3. **IntentLogging** — compta / journal  
4. **x402 PaymentMiddleware** — challenges USDC sur RoutesConfig  
5. **McpAcceptCompat** — complète header `Accept` pour `/mcp*`  
6. **inner_app** — FastAPI + mounts MCP

---

## 4. Routes HTTP payantes (x402)

| Méthode | Path | Prix | Handler |
|---------|------|------|---------|
| POST | `/search` | $0.01 | `handlers/search.py` |
| POST | `/translate` | $0.10 | `handlers/translate.py` |
| POST | `/jobs` | $1.00 | `handlers/jobs.py` |
| POST | `/pdf` | $0.05 | `handlers/pdf.py` |
| POST | `/web-read` | $0.03 | `handlers/web_read.py` |
| POST | `/extract` | $0.05 | `handlers/extract.py` |
| POST | `/summarize` | $0.03 | `handlers/summarize.py` |
| POST | `/fact-check` | $0.05 | `handlers/fact_check.py` |
| POST | `/discover` | $0.001 | `handlers/discover_paid.py` |

**Piège :** `GET /search` existe et **bypass** le paiement (seul `POST /search` est dans RoutesConfig).

---

## 5. Surfaces gratuites (regroupées)

| Domaine | Paths clés |
|---------|------------|
| Salon | `/accueil`, `/salon`, `POST /accueil` |
| Contact | `/contact`, poll `/contact/{id}`, reply auth PC |
| Place | `GET /place`, `GET /place/{slug}` ; écriture auth PC |
| Mesh | `/mesh`, nodes, bounties, claim, ledger |
| Discover gratuit | `GET /discover` (plafond 10), `/discover/sample` |
| Kit / samples | `*/sample`, `/detect-language`, `/capabilities`, digests |
| Trust kit | `/.well-known/{relationship-memory,coordination-thread-*,return-visit-pledge,tool-delivery-receipt,honest-delivery-refusal,agent-trust-kit}.json` + validate/store/retrieve |
| Discovery | `/`, agent cards, `/.well-known/x402*`, MCP cards, ARD, glama, oauth stubs, `robots.txt`, `sitemap.xml`, `llms.txt` |
| Admin | `/admin*`, usine, PC listener (Basic / token) |
| Santé | `/health`, `/favicon.ico` |

Usine générée : `app/generated/routes_registry.yaml` → **`routes: []`** (aucune route dynamique).

---

## 6. Serveurs MCP

| Mount | Fichier | Nature |
|-------|---------|--------|
| `/mcp` | `mcp_server.py` | Principal — tools payants (verify/settle **dans** le tool) + gratuits |
| `/mcp/relationship-memory` | `mcp_relationship_memory.py` | validate / store / retrieve |
| `/mcp/coordination-thread` | `mcp_coordination_thread.py` | validate / retrieve |

**Tools payants actifs sur `/mcp` :** `translate`, `jobs`, `read_pdf`, `read_web_page`, `extract_structured`, `summarize`, `discover_semantic`.

**Commentés (non exposés) :** `search`, `fact_check` — alors que les routes HTTP existent.

---

## 7. Upstream & stockage

| Dépendance | Rôle |
|------------|------|
| OpenRouter | translate, summarize, extract, jobs, samples LLM |
| Ollama (`gemma3:4b` + embeddings) | fact-check, discover |
| SearXNG (`searxng-kairos`) | search, fact-check |
| WebFetch (httpx + trafilatura) | web-read, extract, summarize |
| CDP facilitator | verify/settle x402 |
| SQLite `data/requests.db` | tout le durable app |
| `acteurs_mcp.json.z` | index sémantique discover (prod only dans le checkout actuel) |

Tables SQLite notables : `requests`, `jobs`, `contact_messages`, `place`, `mesh_*`, `chain_payments`, `mpp_nonces`, `usine_*`, `proposals`, `quota_cache`, …

---

## 8. Scripts & usine

| Chemin | Rôle |
|--------|------|
| `scripts/build_acteurs_mcp_snapshot.py` | Construit le snapshot MCP |
| `scripts/catalogue_sync.py` | Sync Bazaar CDP |
| `scripts/comptable.py` / `controleur.py` / `fossoyeur.py` | Ops / audit / retrait |
| `scripts/sepolia_*_e2e_test.py` | E2E paiement (hors CI) |
| `outils/fusionner_env.py` | Fusion safe `.env` |
| `usine/{prospecteur,ouvrier,crieur}.md` | Rôles admin usine |
| `send_daily_report.py` | Rapport mail |

---

## 9. Tests

- **26** fichiers `tests/test_*.py`  
- Dernière run locale : **64 passed / 3 failed**  
  - MCP initialize (FastMCP lifespan) ×2  
  - `test_post_accueil` (200 vs 201)  
- CI : `.github/workflows/ci.yml` → `uv run pytest`  
- Non couvert : handlers payants lourds, admin, mesh HTTP, MPP, capacity, e2e Sepolia

---

## 10. Local ↔ prod (écart résiduel)

| Item | Local | Prod |
|------|-------|------|
| Code (hors websiteUrl RM) | Aligné (`4424969`) | `/opt/x402/app` |
| `websiteUrl` MCP RM | `…/manifeste-…` (200) | encore `…/guide-…` (404) |
| `acteurs_mcp.json.z` | absent | 35 Mo |
| `.env` | secrets non commités | présent sur VPS |

---

## 11. Verdict d’audit

**Ce qui est solide**
- Surface large et cohérente : payant + gratuit + discovery + 3 MCP  
- Prod healthy, nginx + Docker + SearXNG en place  
- Contenu place réel (cartes, goulots, manifeste)  
- Snapshot discover présent en prod  

**Ce qui fuit / reste fragile**
- **Revenu = 0** (`chain_payments` vide) malgré 973 requêtes journalisées (surtout unpaid / probes)  
- `GET /search` non payant = trou dans le modèle économique  
- Tools MCP `search` / `fact_check` désactivés vs HTTP  
- `guide-relationship-memory-agents` 404 encore référencé côté prod  
- README trop court vs réalité (MCP, MPP, admin, trust-kit)  
- 3 tests locaux rouges (lifespan MCP / statut accueil)  
- `numpy` utilisé par discover_paid sans déclaration claire dans deps (à vérifier)  
- Usine / registry vide : pipeline de génération inactif  

**Hors périmètre (machine kairos, arrêté)**  
Le daemon Kairos (`kairos.service`, labo, omniroute) est **stoppé/désactivé** ; il n’écrit plus vers ce service tant qu’il n’est pas relancé.
