from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.coordination_thread import SAMPLE_TURN, json_schema, validate_turn

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
    normalized, errors = validate_turn(body.turn)
    return {
        "valid": not errors,
        "v": 1,
        "errors": errors,
        "normalized": normalized,
    }
