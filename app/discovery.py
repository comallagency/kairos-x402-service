from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse

from app import config
from app.x402_setup import KIT_TAGLINE, build_route_configs, resolve_payment_requirements

router = APIRouter()

# Generated from build_route_configs() rather than hand-maintained as a static
# file, so /.well-known/x402 can never drift from what the payment middleware
# actually enforces.


def _route_entries() -> list[dict]:
    entries = []
    for key, route_config in build_route_configs().items():
        method, path = key.split(" ", 1)
        accepts = route_config.accepts
        if not isinstance(accepts, list):
            accepts = [accepts]
        entries.append(
            {
                "resource": f"{config.BASE_URL}{path}",
                "method": method,
                "description": route_config.description,
                "mimeType": route_config.mime_type,
                "serviceName": route_config.service_name,
                "tags": route_config.tags,
                "accepts": [
                    {
                        "scheme": r.scheme,
                        "network": r.network,
                        "asset": r.asset,
                        "amount": str(r.amount),
                        "price": a.price,
                        "payTo": r.pay_to,
                        "maxTimeoutSeconds": r.max_timeout_seconds,
                        "extra": r.extra,
                    }
                    for a in accepts
                    for r in [resolve_payment_requirements(a)]
                ],
                "extensions": route_config.extensions,
            }
        )
    return entries


@router.get("/.well-known/x402", openapi_extra={"security": []})
async def well_known_x402():
    # GET /discover gratuit en tête : x402watch et d'autres ne lisent que ce
    # document, pas /discovery/resources (mesure 2026-09-18).
    return {
        "x402Version": 2,
        "resources": _route_entries(),
    }


# Le meme document sous l'extension .json - des sondes la demandent (7 hits,
# premiere 2026-09-13) et personne ne la servait. Une seule source de verite :
# le corps de /.well-known/x402, qui est genere depuis build_route_configs()
# et ne peut pas deriver du middleware de paiement.
@router.get("/.well-known/x402.json", openapi_extra={"security": []})
async def well_known_x402_json():
    return await well_known_x402()


def _free_discover_resource_entry() -> dict:
    base = config.BASE_URL.rstrip("/")
    # Mêmes exemples Bazaar que POST /discover : les indexeurs (x402watch,
    # agentic-web) lisent /discovery/resources et /.well-known/x402 — la route
    # GET gratuite n'était listée que sans extensions.
    post_discover = build_route_configs()["POST /discover"]
    return {
        "resource": f"{base}/discover",
        "method": "GET",
        "description": (
            "Free MCP server discovery: semantic ranking over a curated snapshot "
            "(nomic-embed-text). Query param q=your need; 5 matches by default "
            "(max 10 via max_results), no "
            "account, no x402 payment. Bare GET returns a ranked example plus a "
            "hint when q= is omitted."
        ),
        "mimeType": "application/json",
        "serviceName": "AgentIndex Discover (free snapshot)",
        "tags": ["mcp discovery", "semantic search", "server discovery", "free"],
        "accepts": [],
        "extensions": post_discover.extensions,
    }


# BrickBlueBot/0.1 (+https://brick.blue/bot) et d'autres indexeurs agentic-web
# sonent /discovery/resources sur l'hôte du service (404 mesuré 2026-09-17).
# Même forme que l'API CDP discovery/resources — routes payantes de build_route_configs().
@router.get("/discovery/resources", openapi_extra={"security": []})
async def discovery_resources():
    return {
        "x402Version": 2,
        "items": _route_entries(),
    }


# VerifyMCP-OwnersBot/1.0 et d'autres sondes (7 hits en six jours, 2026-09-17).
# Spec : https://verifymcp.io/docs/build/owners-json
@router.get("/.well-known/brick-blue.json", openapi_extra={"security": []})
async def well_known_brick_blue_json():
    """Preuve de domaine pour brick.blue passport (spec : clé publique base58)."""
    key = config.BRICK_BLUE_PUBLIC_KEY
    if not key:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="brick-blue key not configured")
    return {"key": key}


@router.get("/.well-known/owners.json", openapi_extra={"security": []})
async def well_known_owners_json():
    schema_key = "$" + "schema"
    return {
        schema_key: "https://verifymcp.io/schemas/owners.json",
        "owners": ["kairos@comallagency.com", "comallagency@gmail.com"],
    }


# Official MCP registry manifest (server.json) — also probed at origin root.
@router.get("/server.json", openapi_extra={"security": []})
async def server_json_manifest():
    base = config.BASE_URL.rstrip("/")
    schema_key = "$" + "schema"
    return {
        schema_key: "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json",
        "name": "world.agentindex/x402",
        "title": "AgentIndex x402",
        "description": (
            "Pay-per-call web search, translation and research jobs for AI agents. "
            "USDC on Base, no account."
        ),
        "version": "1.1.0",
        "websiteUrl": f"{base}/openapi.json",
        "remotes": [
            {"type": "streamable-http", "url": f"{base}/mcp/"},
        ],
    }


# 402 Index domain ownership proof — hash from POST /api/v1/claim.
@router.get(
    "/.well-known/402index-verify.txt",
    response_class=PlainTextResponse,
    openapi_extra={"security": []},
)
async def well_known_402index_verify():
    token = (getattr(config, "INDEX_402_VERIFY_HASH", None) or "").strip()
    if not token:
        raise HTTPException(status_code=404, detail="402index verify hash not configured")
    return token + "\n"


# Sondes Glama et crawlers MCP (8+ hits en six jours, 2026-09-18). Même schéma
# que glama.json à la racine d'un dépôt — ici exposé au well-known qu'ils sonent.
def _glama_server_card() -> dict:
    schema_key = "$" + "schema"
    base = config.BASE_URL.rstrip("/")
    return {
        schema_key: "https://glama.ai/mcp/schemas/server.json",
        "maintainers": ["comallagency"],
        "name": "AgentIndex x402",
        "repository": "https://github.com/comallagency/kairos-x402-service",
        "description": (
            "Pay-per-call agent toolkit (search, pdf, web-read, extract, summarize, "
            f"fact-check, translate, jobs) plus paid POST {base}/discover "
            "($0.001 USDC via x402, semantic MCP matches). OpenAPI + /.well-known/x402."
        ),
    }


@router.get("/.well-known/glama.json", openapi_extra={"security": []})
async def well_known_glama_json():
    return _glama_server_card()


# Quelques crawlers tapent /glama.json à la racine (journal nginx, 2026-09-18).
@router.get("/glama.json", openapi_extra={"security": []})
async def root_glama_json():
    return _glama_server_card()


# agentprobe/0.1 et registres ARD (7+ hits, 2026-09-09 → 2026-09-18) sonent
# /.well-known/ai-catalog.json et /.well-known/ard.json — même enveloppe ARD v1.
def _ai_catalog_manifest() -> dict:
    base = config.BASE_URL.rstrip("/")
    host_id = "x402.agentindex.world"
    return {
        "specVersion": "1.0",
        "host": {
            "displayName": "Kairos AgentIndex x402",
            "identifier": host_id,
            "documentationUrl": f"{base}/llms.txt",
        },
        "entries": [
            {
                "identifier": f"urn:air:{host_id}:mcp:agentindex-x402",
                "displayName": "AgentIndex x402 MCP",
                "type": "application/mcp-server-card+json",
                "url": f"{base}/.well-known/mcp/server-card.json",
                "description": (
                    "Pay-per-call x402 toolkit (search, pdf, web-read, extract, "
                    "summarize, fact-check, translate, jobs) plus paid POST "
                    f"{base}/discover (0.001 USDC, semantic MCP matches)."
                ),
                "tags": ["mcp", "x402", "discovery", "pay-per-call"],
                "representativeQueries": [
                    "pay-per-call web search USDC Base x402",
                    "semantic MCP server discovery without an account",
                ],
            },
            {
                "identifier": f"urn:air:{host_id}:agent:kairos",
                "displayName": "Kairos A2A agent card",
                "type": "application/a2a-agent-card+json",
                "url": f"{base}/.well-known/agent-card.json",
                "description": (
                    "Full capability card: paid x402 routes, prices and samples."
                ),
                "tags": ["a2a", "x402", "agent-card"],
            },
            {
                "identifier": f"urn:air:{host_id}:openapi:service",
                "displayName": "AgentIndex OpenAPI 3.1",
                "type": "application/openapi+json",
                "url": f"{base}/openapi.json",
                "description": "Machine-readable API spec for all HTTP routes and prices.",
                "tags": ["openapi", "x402"],
            },
            {
                "identifier": f"urn:air:{host_id}:discover:paid",
                "displayName": "Semantic MCP discovery (paid POST)",
                "type": "application/json",
                "url": f"{base}/discover",
                "description": (
                    "POST {\"q\":\"need\"} — ranked MCP matches from a curated "
                    "snapshot. $0.001 USDC via x402. Sample: GET /discover/sample."
                ),
                "tags": ["discovery", "mcp", "x402"],
                "representativeQueries": [
                    "which MCP registries crawl and score server reliability",
                ],
            },
        ],
    }


@router.get("/.well-known/ai-catalog.json", openapi_extra={"security": []})
@router.get("/.well-known/ard.json", openapi_extra={"security": []})
async def well_known_ai_catalog():
    return _ai_catalog_manifest()


# Crawlers and discovery bots probe /robots.txt before anything else - it
# was 404ing (3 hits/day in the vhost log, 2026-09-12), which meant they never
# got a chance to find /llms.txt below. Allow everything and point at it.
@router.get("/robots.txt", response_class=PlainTextResponse, openapi_extra={"security": []})
async def robots_txt():
    base = config.BASE_URL.rstrip("/")
    return (
        "User-agent: *\n"
        "Allow: /\n"
        f"Sitemap: {base}/sitemap.xml\n"
        f"\n# Agent guidance: {base}/llms.txt\n"
    )


def _sitemap_urls() -> list[str]:
    base = config.BASE_URL.rstrip("/")
    paths = [
        "/",
        "/discover/sample",
        "/search/sample",
        "/wallet-balance/sample",
        "/gas-price/sample",
        "/wallet-intelligence/sample",
        "/x402-echo/sample",
        "/agent-health/sample",
        "/agent.json",
        "/capabilities",
        "/openapi.json",
        "/llms.txt",
        "/skills/agentindex-x402/SKILL.md",
        "/server.json",
        "/.well-known/x402",
        "/.well-known/x402.json",
        "/.well-known/ai-catalog.json",
        "/.well-known/ard.json",
        "/.well-known/agent.json",
        "/.well-known/mcp.json",
        "/.well-known/glama.json",
    ]
    return [f"{base}{p}" for p in paths]


@router.get("/sitemap.xml", response_class=PlainTextResponse, openapi_extra={"security": []})
async def sitemap_xml():
    """Crawlers demandent /sitemap.xml (404 mesuré sur le vhost, 2026-09-18)."""
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for loc in _sitemap_urls():
        lines.append("  <url><loc>{}</loc></url>".format(loc))
    lines.append("</urlset>")
    return "\n".join(lines) + "\n"


@router.get("/llms.txt", response_class=PlainTextResponse, openapi_extra={"security": []})
async def llms_txt():
    """Full agent-readable catalog — Hermes / OpenClaw / PipRail discoverers read this."""
    return _llms_catalog()


def _llms_catalog() -> str:
    base = config.BASE_URL.rstrip("/")
    lines = [
        "# AgentIndex x402",
        "",
        f"> Pay-per-call tools for AI agents. USDC on Base via HTTP 402 (x402).",
        f"> No account. No API key. Price from $0.001. Home: {base}",
        "",
        "## How to pay (Hermes, OpenClaw, PipRail, any x402 client)",
        "",
        "1. GET or POST a paid URL below — you receive HTTP 402 + Payment-Required.",
        "2. Sign the exact USDC amount (EIP-3009) and retry with Payment-Signature.",
        "3. Or use PipRail: `piprail_quote_payment` → `piprail_pay_request` on the URL.",
        "4. Or mount our MCP: " + f"{base}/mcp/ (tools: weather, crypto, news, can_pay, probe, …).",
        "",
        "## Cheapest first calls ($0.001 USDC on Base) — start here",
        "",
        f"- [Wallet Balance]({base}/wallet-balance?address=0xYOUR&network=base): native + USDC balances on five EVM networks",
        f"- [Gas Price]({base}/gas-price?network=base): gas, base fee and transfer-cost estimate on five EVM networks",
        f"- [x402 Mainnet Echo]({base}/x402-echo?message=hello): complete real settlement test at the cheapest available price ($0.001)",
        f"- [Agent Health]({base}/agent-health?url=https://example.com): live operational, x402, MCP/A2A and discovery audit ($0.001)",
        f"- [Wallet Intelligence]({base}/wallet-intelligence?address=0xYOUR): one $0.001 call replaces wallet+gas reads on five EVM networks",
        "",
        "## Other live data ($0.001 USDC)",
        "",
        f"- [Can Pay]({base}/can-pay?address=0xYOUR&amount=0.001): check Base USDC balance before spending",
        f"- [Probe]({base}/probe?url=https://example.com): detect if a URL is an x402 paywall + price",
        f"- [Weather]({base}/weather?city=Paris): current weather + 3-day forecast",
        f"- [Crypto]({base}/crypto?coins=btc,eth): live spot prices + 24h change",
        f"- [News]({base}/news?limit=10): top Hacker News headlines",
        f"- [Discover]({base}/discover): POST JSON `{{\"q\":\"need\"}}` — semantic MCP server search",
        "",
        "## High-value web and document tools",
        "",
        f"- [Search + Content]({base}/search): POST `{{\"query\":\"...\",\"include_content\":true}}` — live results plus clean Markdown from the top 3 pages (launch price $0.001)",
        f"- [PDF to Markdown]({base}/pdf): POST `{{\"url\":\"https://...pdf\"}}` — text, metadata and token count ($0.002)",
        f"- [Web Read]({base}/web-read): POST `{{\"url\":\"https://...\"}}` — main content as clean Markdown ($0.002)",
        f"- [Structured Extract]({base}/extract): POST URL/text plus JSON schema ($0.003)",
        f"- [Summarize]({base}/summarize): POST URL/text/HTML ($0.003)",
        "",
        "## Free samples (no payment)",
        "",
        f"- {base}/can-pay/sample",
        f"- {base}/probe/sample",
        f"- {base}/wallet-balance/sample",
        f"- {base}/gas-price/sample",
        f"- {base}/wallet-intelligence/sample",
        f"- {base}/x402-echo/sample",
        f"- {base}/agent-health/sample",
        f"- {base}/weather/sample",
        f"- {base}/crypto/sample",
        f"- {base}/news/sample",
        f"- {base}/discover/sample",
        "",
        "## Discovery manifests",
        "",
        f"- [OpenAPI]({base}/openapi.json)",
        f"- [Agent card]({base}/agent.json)",
        f"- [Capabilities]({base}/capabilities)",
        f"- [x402 well-known]({base}/.well-known/x402)",
        f"- [MCP server card]({base}/.well-known/mcp/server-card.json)",
        f"- [Hermes/OpenClaw skill]({base}/skills/agentindex-x402/SKILL.md)",
        "",
        "## For Hermes",
        "",
        "```yaml",
        "# ~/.hermes/config.yaml — optional MCP (our paid tools)",
        "mcp_servers:",
        "  agentindex:",
        f"    url: \"{base}/mcp/\"",
        "    enabled: true",
        "```",
        "",
        "With PipRail installed: `piprail_discover(\"weather\")` or pay any URL above.",
        f"Skill: {base}/skills/agentindex-x402/SKILL.md",
        "",
        "## For OpenClaw",
        "",
        "Use x402_search / x402_fetch against the Bazaar, or call the URLs above with autopay.",
        f"Skill: {base}/skills/agentindex-x402/SKILL.md",
        "",
        "## Network",
        "",
        "- Chain: Base mainnet (`eip155:8453`)",
        "- Asset: USDC `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`",
        f"- Facilitator: CDP (Coinbase)",
        "",
    ]
    return "\n".join(lines) + "\n"


@router.get(
    "/skills/agentindex-x402/SKILL.md",
    response_class=PlainTextResponse,
    openapi_extra={"security": []},
)
async def agentindex_skill_md():
    from pathlib import Path

    path = Path(__file__).resolve().parent / "skills" / "agentindex-x402" / "SKILL.md"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="skill not found")
    return path.read_text(encoding="utf-8")



def _agent_card() -> dict:
    """Paid kit only — no free salons / place / trust-kit discourse."""
    skills = []
    for route_key, route_config in build_route_configs().items():
        method, path = route_key.split(" ", 1)
        slug = path.lstrip("/")
        payment_option = route_config.accepts
        if isinstance(payment_option, list):
            payment_option = payment_option[0]
        bazaar_info = (route_config.extensions or {}).get("bazaar", {}).get("info", {})
        skills.append(
            {
                "id": slug,
                "name": route_config.service_name,
                "resource": f"{config.BASE_URL}{path}",
                "method": method,
                "price": payment_option.price,
                "description": route_config.description,
                "sample": f"{config.BASE_URL}{path}/sample",
                "input_example": bazaar_info.get("input", {}).get("body"),
                "output_example": bazaar_info.get("output", {}).get("example"),
            }
        )
    skills.sort(key=lambda s: s["id"])
    base = config.BASE_URL.rstrip("/")
    return {
        "name": "AgentIndex x402",
        "description": (
            f"{KIT_TAGLINE} Start with GET /wallet-intelligence ($0.001): "
            "one signature replaces wallet and gas reads on five EVM networks. "
            "Cheapest single reads cost $0.001. USDC on Base (x402), no "
            "account, no API key."
        ),
        "url": base,
        "repository": "https://github.com/comallagency/kairos-x402-service",
        "version": "1.1.0",
        "protocolVersion": "0.3.0",
        "x402": {"wellKnown": f"{base}/.well-known/x402"},
        "openapi": f"{base}/openapi.json",
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
            "stateTransitionHistory": False,
        },
        "capabilitiesUrl": f"{base}/capabilities",
        "skills": skills,
    }



# Three paths serve the same card. "/" because a scanner or a curious agent
# hits the bare host before it knows any route exists; "/.well-known/agent.json"
# and "/.well-known/agent-card.json" because that's the two conventions agent
# crawlers actually probe (A2A's draft and current well-known locations) -
# "/agent.json" alone, with neither, was invisible to both.
@router.get("/", openapi_extra={"security": []})
@router.get("/agent.json", openapi_extra={"security": []})
@router.get("/.well-known/agent.json", openapi_extra={"security": []})
@router.get("/.well-known/agent-card.json", openapi_extra={"security": []})
async def agent_json():
    return _agent_card()


# MCP's own well-known convention (distinct from the A2A agent-card above):
# scanners built for MCP discovery probe "/.well-known/mcp/server-card.json"
# specifically, and got a 404 because only the A2A paths were served. The
# actual MCP server is mounted at /mcp (see app/main.py); this just makes it
# discoverable at the path MCP-aware crawlers already look for.
def _mcp_server_card_payload() -> dict:
    base = config.BASE_URL.rstrip("/")
    return {
        "name": "AgentIndex x402",
        "description": (
            f"{KIT_TAGLINE} Pay-per-call MCP tools: translate, jobs, read_pdf, "
            "read_web_page, extract_structured, summarize, fact_check, search, "
            "discover_semantic. USDC on Base (x402), no account, no API key."
        ),
        "url": base,
        "discoverPaid": {
            "url": f"{base}/discover",
            "method": "POST",
            "price_usdc": 0.001,
            "mcp_tool": "discover_semantic",
        },
        "mcpEndpoint": f"{base}/mcp",
        "protocol": "mcp",
        "transport": "streamable-http",
        "x402": {"wellKnown": f"{base}/.well-known/x402"},
        "openapi": f"{base}/openapi.json",
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
            "stateTransitionHistory": False,
        },
        "capabilitiesUrl": f"{base}/capabilities",
    }


@router.get("/.well-known/mcp/server-card.json", openapi_extra={"security": []})
async def mcp_server_card():
    return _mcp_server_card_payload()


# BrickBlueBot et d'autres indexeurs agentic-web sonent ce chemin (404 mesuré
# 2026-09-17) alors que server-card.json répond déjà — même corps, zéro dérive.
@router.get("/.well-known/mcp.json", openapi_extra={"security": []})
async def well_known_mcp_json():
    return _mcp_server_card_payload()


def _relationship_memory_mcp_card() -> dict:
    base = config.BASE_URL.rstrip("/")
    return {
        "name": "Kairos Relationship Memory",
        "title": "Relationship memory MCP (free)",
        "description": (
            "Dedicated MCP server for portable interlocutor cards (v1): "
            "relationship_memory.validate, .store (validate + persist locally), "
            ".retrieve (schema, sample, starter card). No account, no payment."
        ),
        "url": base,
        "mcpEndpoint": f"{base}/mcp/relationship-memory/",
        "protocol": "mcp",
        "transport": "streamable-http",
        "schema": f"{base}/.well-known/relationship-memory.json",
        "guide": f"{base}/place/guide-relationship-memory-agents",
        "http": {
            "validate": f"{base}/relationship-memory/validate",
            "store": f"{base}/relationship-memory/store",
            "retrieve": f"{base}/relationship-memory/retrieve",
        },
        "registry": {
            "official": f"{base}/server-relationship-memory.json",
        },
    }


@router.get(
    "/.well-known/mcp/relationship-memory.json",
    openapi_extra={"security": []},
)
async def well_known_mcp_relationship_memory():
    raise HTTPException(status_code=404, detail="removed")


def _coordination_thread_mcp_card() -> dict:
    base = config.BASE_URL.rstrip("/")
    return {
        "name": "Kairos Coordination Thread",
        "title": "Coordination thread MCP (free)",
        "description": (
            "Dedicated MCP server for multi-agent thread turns (v1): "
            "coordination_thread.validate, .retrieve (schema, sample, starter turn). "
            "No account, no payment."
        ),
        "url": base,
        "mcpEndpoint": f"{base}/mcp/coordination-thread/",
        "protocol": "mcp",
        "transport": "streamable-http",
        "schema": f"{base}/.well-known/coordination-thread-turn.json",
        "guide": f"{base}/place/coordination-thread",
        "http": {
            "validate": f"{base}/coordination-thread/validate",
            "retrieve": f"{base}/coordination-thread/retrieve",
        },
        "registry": {
            "official": f"{base}/server-coordination-thread.json",
        },
    }


@router.get(
    "/.well-known/mcp/coordination-thread.json",
    openapi_extra={"security": []},
)
async def well_known_mcp_coordination_thread():
    raise HTTPException(status_code=404, detail="removed")


@router.get(
    "/server-coordination-thread.json",
    openapi_extra={"security": []},
)
async def server_coordination_thread_manifest():
    raise HTTPException(status_code=404, detail="removed")



@router.get(
    "/server-relationship-memory.json",
    openapi_extra={"security": []},
)
async def server_relationship_memory_manifest():
    raise HTTPException(status_code=404, detail="removed")



# --- MCP OAuth discovery (RFC 9728 / RFC 8414) --------------------------------
# mcpi/probe and MCP clients request these paths; 404s were counted as
# demande-non-servie (7+ hits/route, 2026-09-12..17). Transport does not use
# OAuth bearer — payment is per-tool x402 inside app/mcp_server.py — but HTTP
# MCP still advertises PRM + AS metadata like mcp.neon.tech.


def _base_url() -> str:
    return config.BASE_URL.rstrip("/")


def _oauth_protected_resource_metadata(resource_path: str) -> dict:
    base = _base_url()
    path = resource_path if resource_path.startswith("/") else f"/{resource_path}"
    if path.rstrip("/") == "/mcp":
        resource = f"{base}/mcp"
        name = "AgentIndex x402 MCP"
    elif path == "/":
        resource = f"{base}/"
        name = "AgentIndex x402"
    else:
        resource = f"{base}{path}"
        name = "AgentIndex x402"
    return {
        "resource": resource,
        "authorization_servers": [base],
        "bearer_methods_supported": ["header"],
        "resource_name": name,
        "resource_documentation": f"{base}/llms.txt",
    }


def _oauth_authorization_server_metadata() -> dict:
    base = _base_url()
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "registration_endpoint": f"{base}/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "token_endpoint_auth_methods_supported": ["none", "client_secret_post"],
        "code_challenge_methods_supported": ["S256"],
    }


_OAUTH_NOT_TRANSPORT = (
    "Ce serveur MCP n'exige pas de jeton OAuth au transport. "
    "Les outils payants règlent via x402 (voir /.well-known/x402)."
)


@router.get("/.well-known/oauth-protected-resource/mcp", openapi_extra={"security": []})
async def oauth_prm_mcp():
    return _oauth_protected_resource_metadata("/mcp")


@router.get("/.well-known/oauth-protected-resource", openapi_extra={"security": []})
async def oauth_prm_root():
    return _oauth_protected_resource_metadata("/")


@router.get("/.well-known/oauth-authorization-server", openapi_extra={"security": []})
@router.get("/.well-known/oauth-authorization-server/mcp", openapi_extra={"security": []})
async def oauth_authorization_server_metadata():
    return _oauth_authorization_server_metadata()


@router.get("/oauth/authorize", openapi_extra={"security": []})
async def oauth_authorize():
    raise HTTPException(
        status_code=400,
        detail=_OAUTH_NOT_TRANSPORT,
    )


@router.post("/oauth/token", openapi_extra={"security": []})
async def oauth_token():
    raise HTTPException(
        status_code=400,
        detail=_OAUTH_NOT_TRANSPORT,
    )


@router.post("/oauth/register", openapi_extra={"security": []})
async def oauth_register():
    raise HTTPException(
        status_code=501,
        detail=_OAUTH_NOT_TRANSPORT,
    )
