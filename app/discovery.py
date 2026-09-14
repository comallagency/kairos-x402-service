from fastapi import APIRouter
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
    return {"x402Version": 2, "resources": _route_entries()}


# Crawlers and discovery bots probe /robots.txt before anything else - it
# was 404ing (3 hits/day in the vhost log, 2026-09-12), which meant they never
# got a chance to find /llms.txt below. Allow everything and point at it.
@router.get("/robots.txt", response_class=PlainTextResponse, openapi_extra={"security": []})
async def robots_txt():
    return (
        "User-agent: *\n"
        "Allow: /\n"
        f"\n# Agent guidance: {config.BASE_URL}/llms.txt\n"
    )


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
        skills.append(
            {
                "id": slug,
                "name": route_config.service_name,
                "resource": f"{config.BASE_URL}{path}",
                "method": method,
                "price": payment_option.price,
                "description": route_config.description,
                "sample": f"{config.BASE_URL}{path}/sample",
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
        },
    )
    skills.insert(
        1,
        {
            "id": "discover",
            "name": "MCP server discovery",
            "resource": f"{config.BASE_URL}/discover",
            "method": "GET",
            "price": "free",
            "description": (
                "Find MCP servers matching a need, ranked by semantic relevance "
                "- free, no account, no payment."
            ),
            "sample": f"{config.BASE_URL}/discover/sample",
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
        },
    )

    return {
        "name": "AgentIndex x402",
        "description": (
            f"{KIT_TAGLINE} A pay-per-call kit (pdf, web-read, extract, "
            "summarize, detect-language) plus batch translation and "
            "delegated research jobs. USDC on Base, no account, no API key."
        ),
        "url": config.BASE_URL,
        "version": "1.0.0",
        "x402": {"wellKnown": f"{config.BASE_URL}/.well-known/x402"},
        "openapi": f"{config.BASE_URL}/openapi.json",
        "capabilities": f"{config.BASE_URL}/capabilities",
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
@router.get("/.well-known/mcp/server-card.json", openapi_extra={"security": []})
async def mcp_server_card():
    return {
        "name": "AgentIndex x402",
        "description": (
            f"{KIT_TAGLINE} A pay-per-call kit (pdf, web-read, extract, "
            "summarize, detect-language) plus batch translation and "
            "delegated research jobs. USDC on Base, no account, no API key."
        ),
        "url": config.BASE_URL,
        "mcpEndpoint": f"{config.BASE_URL}/mcp",
        "protocol": "mcp",
        "transport": "streamable-http",
        "x402": {"wellKnown": f"{config.BASE_URL}/.well-known/x402"},
        "openapi": f"{config.BASE_URL}/openapi.json",
        "capabilities": f"{config.BASE_URL}/capabilities",
    }
