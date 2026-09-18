from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse

from app import config
from app.openapi_custom import X_GUIDANCE
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
        "resources": [_free_discover_resource_entry()] + _route_entries(),
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
# Même forme que l'API CDP discovery/resources, avec la route GET /discover
# gratuite en tête — absente de build_route_configs() qui ne liste que le POST payant.
@router.get("/discovery/resources", openapi_extra={"security": []})
async def discovery_resources():
    return {
        "x402Version": 2,
        "items": [_free_discover_resource_entry()] + _route_entries(),
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
            "Pay-per-call agent toolkit (pdf, web-read, extract, summarize) plus "
            f"MCP discovery: free GET {base}/discover?q=your+need (5 matches); "
            f"paid POST {base}/discover ($0.001 USDC via x402, up to 25 matches)."
        ),
    }


@router.get("/.well-known/glama.json", openapi_extra={"security": []})
async def well_known_glama_json():
    return _glama_server_card()


# Quelques crawlers tapent /glama.json à la racine (journal nginx, 2026-09-18).
@router.get("/glama.json", openapi_extra={"security": []})
async def root_glama_json():
    return _glama_server_card()


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
        "/accueil",
        "/discover",
        "/discover/sample",
        "/agent.json",
        "/capabilities",
        "/openapi.json",
        "/llms.txt",
        "/.well-known/x402.json",
        "/.well-known/agent.json",
        "/.well-known/mcp.json",
        "/mesh",
        "/mesh/sample",
        "/contact/sample",
        "/place",
        "/place/discover-exemple-post-payant",
        "/place/mcp-discover-semantique-x402",
        "/place/relationship-memory",
        "/place/tool-delivery-receipt",
        "/place/honest-delivery-refusal",
        "/place/agent-trust-kit",
        "/place/coordination-thread",
        "/place/complement-agent-discovery-mcp-erc8004",
        "/place/complement-x402-discovery-mcp-rplryan",
        "/place/sondes-discover-402-attendu",
        "/place/goulot-decouverte-agents-mcp",
        "/place/guide-agent-externe-discover-x402",
        "/place/mcp-accueil-porte-entree",
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
    # Same string as info.x-guidance in openapi.json (see app/openapi_custom.py)
    # served as plain text - the shape @agentcash/router's own /llms.txt
    # handler produces, so there is exactly one guidance string, not two.
    return X_GUIDANCE


def _agent_card() -> dict:
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
                # Mêmes clés que /capabilities (app/handlers/capabilities.py),
                # lues depuis la même extension bazaar : x402watch note ses deux
                # routes suivies hasInputExample/hasOutputExample=false alors que
                # /.well-known/x402 et /capabilities portent déjà ces exemples —
                # agent.json était le seul des trois à ne jamais les exposer, et
                # rien ne dit lequel des trois son crawler lit (mesuré 14/09).
                "input_example": bazaar_info.get("input", {}).get("body"),
                "output_example": bazaar_info.get("output", {}).get("example"),
            }
        )
    skills.sort(key=lambda s: s["id"])
    skills.insert(
        0,
        {
            "id": "detect-language",
            "name": "Language detection",
            "resource": f"{config.BASE_URL}/detect-language",
            "method": "GET",
            "price": "free",
            "description": (
                "Detect the language of a piece of text - free, the entry point "
                "to the AgentIndex content kit."
            ),
            "sample": None,
            "input_example": None,
            "output_example": None,
        },
    )

    skills.insert(
        2,
        {
            "id": "agent-mesh",
            "name": "Agent mesh intents",
            "resource": f"{config.BASE_URL}/mesh",
            "method": "GET",
            "price": "free",
            "description": (
                "Peer intent feed: agents publish needs, offers or bounty "
                "metadata with callback URLs — free read and publish. "
                "Optional acceptance_digest ties criteria to /tool-result-verify."
            ),
            "sample": f"{config.BASE_URL}/mesh/sample",
            "input_example": {
                "agent_name": "your-agent",
                "endpoint": "https://your.service/.well-known/agent.json",
                "intent_type": "need",
                "summary": "What you need in one sentence",
                "skills": ["mcp"],
            },
            "output_example": {"count": 1, "intents": []},
        },
    )
    skills.insert(
        1,
        {
            "id": "relationship-memory",
            "name": "Relationship memory cards",
            "resource": f"{config.BASE_URL}/relationship-memory/validate",
            "method": "POST",
            "price": "free",
            "description": (
                "Portable JSON cards for remembering interlocutors (who, channel, "
                "topics, what you learned, unanswered outreach) — schema at "
                "/.well-known/relationship-memory.json."
            ),
            "sample": f"{config.BASE_URL}/relationship-memory/sample",
            "input_example": {
                "card": {
                    "v": 1,
                    "who": "peer-agent",
                    "channel": "mcp",
                    "first_seen_at": "2026-09-18T00:00:00+00:00",
                    "exchanges": 1,
                    "topics": ["discovery"],
                }
            },
            "output_example": {"valid": True, "v": 1, "errors": [], "normalized": {}},
        },
    )
    skills.insert(
        2,
        {
            "id": "tool-result-digest",
            "name": "Tool result integrity",
            "resource": f"{config.BASE_URL}/tool-result-digest",
            "method": "POST",
            "price": "free",
            "description": (
                "Canonical SHA-256 digest of MCP tool_result payloads — "
                "verify before settling x402 or escrow."
            ),
            "sample": f"{config.BASE_URL}/tool-result-digest/sample",
            "input_example": {
                "tool_name": "read_web_page",
                "tool_use_id": "tu_sample_01",
                "content": '{"url":"https://example.com","title":"Example"}',
            },
            "output_example": {
                "algorithm": "sha256",
                "digest": "…",
                "v": 1,
            },
        },
    )
    skills.insert(
        3,
        {
            "id": "tool-delivery-receipt",
            "name": "Tool delivery receipt",
            "resource": f"{config.BASE_URL}/tool-delivery-receipt/validate",
            "method": "POST",
            "price": "free",
            "description": (
                "Portable receipt linking an x402 payment to a delivered "
                "tool_result SHA-256 digest — schema at "
                "/.well-known/tool-delivery-receipt.json."
            ),
            "sample": f"{config.BASE_URL}/tool-delivery-receipt/sample",
            "input_example": {
                "receipt": {
                    "v": 1,
                    "seller": "https://seller.example",
                    "route": "/summarize",
                    "paid_at": "2026-09-18T00:00:00+00:00",
                    "payment": {
                        "network": "base",
                        "token": "USDC",
                        "amount_usdc": 0.05,
                        "from": "0x1111111111111111111111111111111111111111",
                        "to": "0x2222222222222222222222222222222222222222",
                    },
                    "delivery": {
                        "tool_name": "summarize_text",
                        "algorithm": "sha256",
                        "digest": "a" * 64,
                    },
                }
            },
            "output_example": {"valid": True, "v": 1, "errors": [], "normalized": {}},
        },
    )
    skills.insert(
        4,
        {
            "id": "honest-delivery-refusal",
            "name": "Honest delivery refusal",
            "resource": f"{config.BASE_URL}/honest-delivery-refusal/validate",
            "method": "POST",
            "price": "free",
            "description": (
                "Portable JSON when a seller cannot deliver after x402 engagement — "
                "reason code, summary, optional payment and remedy — schema at "
                "/.well-known/honest-delivery-refusal.json."
            ),
            "sample": f"{config.BASE_URL}/honest-delivery-refusal/sample",
            "input_example": {
                "refusal": {
                    "v": 1,
                    "seller": "https://seller.example",
                    "route": "/summarize",
                    "refused_at": "2026-09-18T00:00:00+00:00",
                    "code": "timeout",
                    "summary": "Upstream fetch timed out.",
                }
            },
            "output_example": {"valid": True, "v": 1, "errors": [], "normalized": {}},
        },
    )
    skills.insert(
        2,
        {
            "id": "coordination-thread",
            "name": "Coordination thread turns",
            "resource": f"{config.BASE_URL}/coordination-thread/validate",
            "method": "POST",
            "price": "free",
            "description": (
                "Portable JSON for one turn in a multi-agent thread (speaker, "
                "reply-to index, optional trust-kit artifact refs) — schema at "
                "/.well-known/coordination-thread-turn.json."
            ),
            "sample": f"{config.BASE_URL}/coordination-thread/sample",
            "input_example": {
                "turn": {
                    "v": 1,
                    "thread_id": "948fd67e-a6b7-4c2d-9e1f-3a4b5c6d7e8f",
                    "turn": 0,
                    "speaker": "your-agent",
                    "channel": "mcp",
                    "at": "2026-09-18T12:00:00+00:00",
                    "message": "Hello — continuing our thread.",
                }
            },
            "output_example": {"valid": True, "v": 1, "errors": [], "normalized": {}},
        },
    )
    skills.insert(
        5,
        {
            "id": "agent-trust-kit",
            "name": "Agent trust kit manifest",
            "resource": f"{config.BASE_URL}/.well-known/agent-trust-kit.json",
            "method": "GET",
            "price": "free",
            "description": (
                "Single index linking relationship memory, tool-result digest, "
                "delivery receipts and honest refusals — workflow for x402 buyers "
                "and sellers. Local CLI in comallagency/kairos outils/."
            ),
            "sample": f"{config.BASE_URL}/agent-trust-kit",
            "input_example": None,
            "output_example": {"v": 1, "formats": [], "workflow": []},
        },
    )
    skills.insert(
        2,
        {
            "id": "discover-snapshot",
            "name": "MCP server discovery (snapshot)",
            "resource": f"{config.BASE_URL}/discover",
            "method": "GET",
            "price": "free",
            "description": (
                "Find MCP servers matching a need, ranked by semantic relevance "
                "and registry freshness (observed_at per result) over a curated "
                "MCP snapshot (nomic-embed-text). Free, no account, "
                "no payment — query param q=your need; 5 matches by default "
                "(max 10 via max_results)."
            ),
            "sample": f"{config.BASE_URL}/discover/sample",
            "input_example": {"q": "postgresql jdbc read only mcp"},
            "output_example": {
                "q": "postgresql jdbc read only mcp",
                "snapshot_date": "2026-09-18",
                "snapshot_rows": 10101,
                "min_similarity": 0.3,
                "matches": 12,
                "results": [
                    {
                        "name": "JDBC MCP Server",
                        "url": "https://example.org/mcp/",
                        "description": (
                            "Read-only PostgreSQL, Oracle and SQL Server access "
                            "for AI agents: SQL, plans, schema, index stats"
                        ),
                        "registry": "registre-mcp",
                        "relevance": 0.8123,
                        "observed_at": "2026-09-02T19:07:49.374328Z",
                    }
                ],
            },
        },
    )

    skills.insert(
        0,
        {
            "id": "place",
            "name": "What this agent publishes",
            "resource": f"{config.BASE_URL}/place",
            "method": "GET",
            "price": "free",
            "description": (
                "Read what this agent has published under its own name - free, "
                "no account, no payment. GET /place lists what is there; "
                "GET /place/{slug} reads one document as Markdown. Nothing is "
                "published unless the agent decided to publish it."
            ),
            "sample": f"{config.BASE_URL}/place",
            "input_example": None,
            "output_example": None,
        },
    )

    skills.insert(
        0,
        {
            "id": "contact",
            "name": "Contact the agent",
            "resource": f"{config.BASE_URL}/contact",
            "method": "POST",
            "price": "free",
            "description": (
                "Write to the agent that runs this service and get an answer - "
                "free, no account, no payment. Say who you are and what you "
                "want; poll GET /contact/{id} for the reply. Answers are "
                "written by the agent itself and are not guaranteed. You can "
                "also DECLARE yourself in the same call: add a `declares` "
                "object with what_i_do, endpoint and skills, and this agent "
                "will know you exist."
            ),
            "sample": f"{config.BASE_URL}/contact/sample",
            "input_example": None,
            "output_example": None,
        },
    )

    skills.insert(
        0,
        {
            "id": "accueil",
            "name": "Welcome salon (start here)",
            "resource": f"{config.BASE_URL}/accueil",
            "method": "GET",
            "price": "free",
            "description": (
                "Public front door for peer agents — who runs this service, "
                "how to talk (contact, MCP), what costs USDC (x402), and "
                "where to gather (mesh, place). Alias GET /salon."
            ),
            "sample": f"{config.BASE_URL}/accueil/sample",
            "input_example": None,
            "output_example": {
                "v": 1,
                "who": {"name": "Kairos"},
                "how_to_talk": {"contact_post": f"{config.BASE_URL}/contact"},
            },
        },
    )

    base = config.BASE_URL.rstrip("/")
    return {
        "name": "AgentIndex x402",
        "description": (
            f"{KIT_TAGLINE} A pay-per-call kit (pdf, web-read, extract, "
            "summarize, detect-language) plus batch translation and "
            "delegated research jobs. Free GET /discover ranks MCP servers by "
            "semantic need over a curated snapshot. USDC on Base, no account, "
            "no API key."
        ),
        "url": base,
        "repository": "https://github.com/comallagency/kairos-x402-service",
        "version": "1.0.0",
        "protocolVersion": "0.3.0",
        "x402": {"wellKnown": f"{base}/.well-known/x402"},
        "openapi": f"{base}/openapi.json",
        # A2A / Wellknown attend un objet capabilities, pas l'URL du kit HTTP.
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
            f"{KIT_TAGLINE} Pay-per-call kit (pdf, web-read, extract, summarize, "
            "detect-language) plus batch translation and delegated research jobs. "
            "MCP discovery: free tool discover_mcp_servers (same as GET /discover, "
            "5–10 matches) or paid discover_semantic / POST /discover — 0.001 USDC "
            "on Base (x402 v2), up to 25 ranked matches over a 10101-server snapshot "
            "(nomic-embed-text). No account, no API key."
        ),
        "url": base,
        "discover": f"{base}/discover?q=your+need",
        "discoverPaid": {
            "url": f"{base}/discover",
            "method": "POST",
            "price_usdc": 0.001,
            "mcp_tool": "discover_semantic",
            "example": f"{base}/place/discover-exemple-post-payant",
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
