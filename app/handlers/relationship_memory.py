from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.relationship_memory import SAMPLE_CARD, json_schema, validate_card

router = APIRouter()

DESCRIPTION = (
    "Validate a portable relationship-memory card (v1) — free, no account. "
    "Agents remember interlocutors, not isolated messages: who, channel, "
    "exchange count, topics, what you learned, unanswered outreach. "
    "Schema at GET /.well-known/relationship-memory.json."
)


class ValidateBody(BaseModel):
    card: dict[str, Any] = Field(..., description="Relationship memory card object")


@router.get(
    "/.well-known/relationship-memory.json",
    openapi_extra={"security": []},
    summary="JSON Schema for agent relationship memory cards — free.",
    tags=["memory", "free", "discovery"],
)
async def well_known_relationship_memory():
    return json_schema()


@router.get(
    "/relationship-memory/sample",
    openapi_extra={"security": []},
    summary="Example relationship memory card — free.",
    tags=["memory", "free", "sample"],
)
async def relationship_memory_sample():
    return {**SAMPLE_CARD, "sample": True}


@router.post(
    "/relationship-memory/validate",
    openapi_extra={"security": []},
    summary="Validate a relationship memory card — free.",
    description=DESCRIPTION,
    tags=["memory", "free"],
)
async def relationship_memory_validate(body: ValidateBody):
    normalized, errors = validate_card(body.card)
    return {
        "valid": not errors,
        "v": 1,
        "errors": errors,
        "normalized": normalized,
    }
