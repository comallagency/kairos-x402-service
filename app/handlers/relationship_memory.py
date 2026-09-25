from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app import config
from app.relationship_memory import (
    SAMPLE_CARD,
    json_schema,
    retrieve_payload,
    store_payload,
    validation_payload,
)

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


def _base_url() -> str:
    return config.BASE_URL.rstrip("/")


def _validate_response(card: dict[str, Any]) -> dict[str, Any]:
    return validation_payload(card)


@router.post(
    "/relationship-memory",
    openapi_extra={"security": []},
    summary="Validate a relationship memory card — free (canonical path).",
    description=DESCRIPTION + " Same semantics as POST /relationship-memory/validate.",
    tags=["memory", "free"],
)
async def relationship_memory_post(body: ValidateBody):
    return _validate_response(body.card)


@router.post(
    "/relationship-memory/validate",
    openapi_extra={"security": []},
    summary="Validate a relationship memory card — free (alias).",
    description=DESCRIPTION,
    tags=["memory", "free"],
)
async def relationship_memory_validate(body: ValidateBody):
    return _validate_response(body.card)


@router.post(
    "/relationship-memory/store",
    openapi_extra={"security": []},
    summary="Validate and return a card to persist locally — free.",
    description=DESCRIPTION
    + " Stateless: nothing is kept server-side; use normalized after valid=true.",
    tags=["memory", "free"],
)
async def relationship_memory_store(body: ValidateBody):
    return store_payload(body.card)


@router.get(
    "/relationship-memory/retrieve",
    openapi_extra={"security": []},
    summary="Schema, sample and MCP/HTTP entrypoints — free.",
    tags=["memory", "free", "discovery"],
)
async def relationship_memory_retrieve(who: str | None = None):
    base = _base_url()
    return retrieve_payload(
        who=who,
        mcp_endpoint=f"{base}/mcp/relationship-memory/",
        http_base=base,
    )
