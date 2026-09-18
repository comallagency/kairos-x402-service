from fastapi import APIRouter

from app.agent_trust_kit import build_manifest

router = APIRouter()


@router.get(
    "/.well-known/agent-trust-kit.json",
    openapi_extra={"security": []},
    summary="Agent trust kit manifest — free discovery index.",
    tags=["trust", "free", "discovery"],
)
async def well_known_agent_trust_kit():
    return build_manifest()


@router.get(
    "/agent-trust-kit",
    openapi_extra={"security": []},
    summary="Same manifest as /.well-known/agent-trust-kit.json — free.",
    tags=["trust", "free", "discovery"],
)
async def agent_trust_kit():
    return build_manifest()
