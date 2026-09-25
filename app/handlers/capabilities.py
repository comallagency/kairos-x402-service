"""GET /capabilities — liste du kit payant uniquement."""

from fastapi import APIRouter

from app.x402_setup import KIT_TAGLINE, ROUTE_SUMMARIES, ROUTE_USE_CASES, build_route_configs

router = APIRouter()

_KIT_SLUGS = {
    "pdf",
    "web-read",
    "extract",
    "summarize",
    "search",
    "fact-check",
    "translate",
    "jobs",
    "discover",
    "weather",
    "crypto",
    "news",
    "can-pay",
    "probe",
    "wallet-balance",
    "gas-price",
    "wallet-intelligence",
    "x402-echo",
    "tip",
    "agent-claim",
    "agent-health",
}


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
    return {
        "name": "AgentIndex content kit",
        "description": KIT_TAGLINE,
        "source": "https://github.com/comallagency/kairos-x402-service",
        "routes": entries,
    }
