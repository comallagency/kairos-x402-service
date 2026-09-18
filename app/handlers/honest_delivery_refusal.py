from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.honest_delivery_refusal import SAMPLE_REFUSAL, json_schema, validate_refusal

router = APIRouter()

DESCRIPTION = (
    "Validate a portable honest-delivery-refusal record (v1) — free, no account. "
    "Structured failure after x402 engagement when no tool_result digest applies. "
    "Schema at GET /.well-known/honest-delivery-refusal.json."
)


class ValidateBody(BaseModel):
    refusal: dict[str, Any] = Field(..., description="Honest delivery refusal object")


@router.get(
    "/.well-known/honest-delivery-refusal.json",
    openapi_extra={"security": []},
    summary="JSON Schema for honest delivery refusals — free.",
    tags=["trust", "free", "discovery"],
)
async def well_known_honest_delivery_refusal():
    return json_schema()


@router.get(
    "/honest-delivery-refusal/sample",
    openapi_extra={"security": []},
    summary="Example honest delivery refusal — free.",
    tags=["trust", "free", "sample"],
)
async def honest_delivery_refusal_sample():
    return {**SAMPLE_REFUSAL, "sample": True}


@router.post(
    "/honest-delivery-refusal/validate",
    openapi_extra={"security": []},
    summary="Validate an honest delivery refusal — free.",
    description=DESCRIPTION,
    tags=["trust", "free"],
)
async def honest_delivery_refusal_validate(body: ValidateBody):
    normalized, errors = validate_refusal(body.refusal)
    return {
        "valid": not errors,
        "v": 1,
        "errors": errors,
        "normalized": normalized,
    }
