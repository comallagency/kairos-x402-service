from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.coordination_snapshot import SAMPLE_SNAPSHOT, json_schema, validate_snapshot

router = APIRouter()

DESCRIPTION = (
    "Validate a coordination thread snapshot (v1) — free, no account. "
    "Bundle turns, relationship cards and open return pledges for handoff "
    "between runs. Schema at GET /.well-known/coordination-thread-snapshot.json."
)


class ValidateBody(BaseModel):
    snapshot: dict[str, Any] = Field(..., description="Coordination thread snapshot object")


@router.get(
    "/.well-known/coordination-thread-snapshot.json",
    openapi_extra={"security": []},
    summary="JSON Schema for coordination thread snapshots — free.",
    tags=["coordination", "free", "discovery"],
)
async def well_known_coordination_thread_snapshot():
    return json_schema()


@router.get(
    "/coordination-thread-snapshot/sample",
    openapi_extra={"security": []},
    summary="Example coordination thread snapshot — free.",
    tags=["coordination", "free", "sample"],
)
async def coordination_thread_snapshot_sample():
    return {**SAMPLE_SNAPSHOT, "sample": True}


@router.post(
    "/coordination-thread-snapshot/validate",
    openapi_extra={"security": []},
    summary="Validate a coordination thread snapshot — free.",
    description=DESCRIPTION,
    tags=["coordination", "free"],
)
async def coordination_thread_snapshot_validate(body: ValidateBody):
    normalized, errors = validate_snapshot(body.snapshot)
    return {
        "valid": not errors,
        "v": 1,
        "errors": errors,
        "normalized": normalized,
    }
