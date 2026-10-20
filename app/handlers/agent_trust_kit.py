from fastapi import APIRouter

from app.agent_trust_kit import manifest

router = APIRouter()

DESCRIPTION = (
    "Machine-readable index of Kairos free trust formats (relationship memory, "
    "tool_result digest, delivery receipt, honest refusal, coordination thread, "
    "return pledge) with schema URLs, validators and suggested workflows."
)


@router.get(
    "/.well-known/agent-trust-kit.json",
    openapi_extra={"security": []},
    summary="Agent trust kit manifest — free.",
    description=DESCRIPTION,
    tags=["trust", "free", "discovery"],
)
async def well_known_agent_trust_kit():
    return manifest()


@router.get(
    "/agent-trust-kit",
    openapi_extra={"security": []},
    summary="Agent trust kit manifest (alias) — free.",
    description=DESCRIPTION,
    tags=["trust", "free", "discovery"],
)
async def agent_trust_kit_alias():
    return manifest()
