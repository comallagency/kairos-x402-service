from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.tool_delivery_receipt import SAMPLE_RECEIPT, json_schema, validate_receipt

router = APIRouter()

DESCRIPTION = (
    "Validate a portable tool-delivery receipt (v1) — free, no account. "
    "Links an x402 payment to a SHA-256 tool_result digest. "
    "Optional body field `content` re-computes the digest locally. "
    "Schema at GET /.well-known/tool-delivery-receipt.json."
)


class ValidateBody(BaseModel):
    receipt: dict[str, Any] = Field(..., description="Tool delivery receipt object")
    content: Any | None = Field(
        None,
        description="Optional tool_result content to verify delivery.digest",
    )


@router.get(
    "/.well-known/tool-delivery-receipt.json",
    openapi_extra={"security": []},
    summary="JSON Schema for agent tool delivery receipts — free.",
    tags=["trust", "free", "discovery"],
)
async def well_known_tool_delivery_receipt():
    return json_schema()


@router.get(
    "/tool-delivery-receipt/sample",
    openapi_extra={"security": []},
    summary="Example tool delivery receipt — free.",
    tags=["trust", "free", "sample"],
)
async def tool_delivery_receipt_sample():
    return {**SAMPLE_RECEIPT, "sample": True}


@router.post(
    "/tool-delivery-receipt/validate",
    openapi_extra={"security": []},
    summary="Validate a tool delivery receipt — free.",
    description=DESCRIPTION,
    tags=["trust", "free"],
)
async def tool_delivery_receipt_validate(body: ValidateBody):
    normalized, errors, digest_check = validate_receipt(
        body.receipt, content=body.content
    )
    out: dict[str, Any] = {
        "valid": not errors,
        "v": 1,
        "errors": errors,
        "normalized": normalized,
    }
    if digest_check is not None:
        out["digest_verification"] = digest_check
    return out
