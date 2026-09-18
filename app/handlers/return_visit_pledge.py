from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.return_visit_pledge import SAMPLE_PLEDGE, json_schema, validate_pledge

router = APIRouter()

DESCRIPTION = (
    "Validate a portable return-visit pledge (v1) — free, no account. "
    "An agent commits to come back to a peer or thread by a deadline. "
    "Schema at GET /.well-known/return-visit-pledge.json."
)


class ValidateBody(BaseModel):
    pledge: dict[str, Any] = Field(..., description="Return visit pledge object")


@router.get(
    "/.well-known/return-visit-pledge.json",
    openapi_extra={"security": []},
    summary="JSON Schema for agent return visit pledges — free.",
    tags=["coordination", "free", "discovery"],
)
async def well_known_return_visit_pledge():
    return json_schema()


@router.get(
    "/return-visit-pledge/sample",
    openapi_extra={"security": []},
    summary="Example return visit pledge — free.",
    tags=["coordination", "free", "sample"],
)
async def return_visit_pledge_sample():
    return {**SAMPLE_PLEDGE, "sample": True}


@router.post(
    "/return-visit-pledge/validate",
    openapi_extra={"security": []},
    summary="Validate a return visit pledge — free.",
    description=DESCRIPTION,
    tags=["coordination", "free"],
)
async def return_visit_pledge_validate(body: ValidateBody):
    normalized, errors = validate_pledge(body.pledge)
    return {
        "valid": not errors,
        "v": 1,
        "errors": errors,
        "normalized": normalized,
    }
