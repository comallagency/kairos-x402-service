"""GET /capabilities - free, no payment. Lists the whole content kit with
price and use cases so an agent that found any one kit route can discover
the others. Reads build_route_configs() live, same as everything else -
never a hand-copied second list of "what routes exist"."""

from fastapi import APIRouter

from app.x402_setup import (
    DISCOVER_SAMPLE_OUTPUT,
    KIT_TAGLINE,
    ROUTE_SUMMARIES,
    ROUTE_USE_CASES,
    build_route_configs,
)

router = APIRouter()

_KIT_SLUGS = {"pdf", "web-read", "extract", "summarize", "search", "fact-check", "translate", "jobs", "discover"}


@router.get("/capabilities", openapi_extra={"security": []})
async def capabilities():
    entries = []
    for route_key, route_config in build_route_configs().items():
        method, path = route_key.split(" ", 1)
        slug = path.lstrip("/")
        if slug not in _KIT_SLUGS:
            continue
        payment_option = route_config.accepts
        if isinstance(payment_option, list):
            payment_option = payment_option[0]
        bazaar_info = (route_config.extensions or {}).get("bazaar", {}).get("info", {})
        entries.append(
            {
                "route": path,
                "method": method,
                "price": payment_option.price,
                "summary": ROUTE_SUMMARIES.get(slug),
                "use_cases": ROUTE_USE_CASES.get(slug, []),
                "sample": f"{path}/sample",
                "input_example": bazaar_info.get("input", {}).get("body"),
                "output_example": bazaar_info.get("output", {}).get("example"),
            }
        )
    entries.sort(key=lambda e: e["route"])
    entries.insert(
        0,
        {
            "route": "/detect-language",
            "method": "GET",
            "price": "free",
            "summary": "Detect the language of a piece of text - the kit's free entry point.",
            "use_cases": ["check that the kit is reachable before paying for anything else"],
            "sample": None,
            "input_example": None,
            "output_example": None,
        },
    )
    entries.insert(
        1,
        {
            "route": "/discover",
            "method": "GET",
            "price": "free",
            "summary": (
                "Find MCP servers by need, ranked by semantic relevance over a "
                "curated snapshot — no payment (5 by default, max 10 via max_results)."
            ),
            "use_cases": [
                "discover MCP servers matching a need before wiring a client",
                "free snapshot search when POST /discover payment is not needed",
            ],
            "sample": "/discover/sample",
            "input_example": {"q": "postgresql jdbc read only mcp"},
            "output_example": DISCOVER_SAMPLE_OUTPUT,
        },
    )
    return {
        "name": "AgentIndex content kit",
        "description": KIT_TAGLINE,
        "routes": entries,
    }
