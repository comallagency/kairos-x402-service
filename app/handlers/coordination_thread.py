from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app import config
from app.coordination_thread import (
    SAMPLE_TURN,
    json_schema,
    retrieve_payload,
    validation_payload,
)

router = APIRouter()

DESCRIPTION = (
    "Validate one coordination-thread turn (v1) — free, no account. "
    "Structure multi-agent replies with optional trust-kit artifact refs. "
    "Schema at GET /.well-known/coordination-thread-turn.json."
)


class ValidateBody(BaseModel):
    turn: dict[str, Any] = Field(..., description="Coordination thread turn object")


@router.get(
    "/.well-known/coordination-thread-turn.json",
    openapi_extra={"security": []},
    summary="JSON Schema for coordination thread turns — free.",
    tags=["coordination", "free", "discovery"],
)
async def well_known_coordination_thread_turn():
    return json_schema()


@router.get(
    "/coordination-thread/sample",
    openapi_extra={"security": []},
    summary="Example coordination thread turn — free.",
    tags=["coordination", "free", "sample"],
)
async def coordination_thread_sample():
    return {**SAMPLE_TURN, "sample": True}


@router.post(
    "/coordination-thread/validate",
    openapi_extra={"security": []},
    summary="Validate a coordination thread turn — free.",
    description=DESCRIPTION,
    tags=["coordination", "free"],
)
async def coordination_thread_validate(body: ValidateBody):
    return validation_payload(body.turn)


@router.get(
    "/coordination-thread/retrieve",
    openapi_extra={"security": []},
    summary="Schema, sample and MCP/HTTP entrypoints — free.",
    tags=["coordination", "free", "discovery"],
)
async def coordination_thread_retrieve(
    speaker: str | None = None,
    thread_id: str | None = None,
):
    base = config.BASE_URL.rstrip("/")
    return retrieve_payload(
        speaker=speaker,
        thread_id=thread_id,
        mcp_endpoint=f"{base}/mcp/coordination-thread/",
        http_base=base,
    )
