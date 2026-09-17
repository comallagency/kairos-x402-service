from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.tool_digest import digest_tool_result, verify_tool_result_digest

router = APIRouter()

DESCRIPTION = (
    "Compute a stable SHA-256 digest over an MCP tool_result (or any tool "
    "output) so agents can verify what they received matches what was "
    "promised — free, no account, no payment. Pair with POST "
    "/tool-result-verify before settling x402 or escrow."
)

SAMPLE = {
    "tool_name": "read_web_page",
    "tool_use_id": "tu_sample_01",
    "content": '{"url":"https://example.com","title":"Example","text":"Hello"}',
}


class DigestBody(BaseModel):
    tool_name: str = Field(..., min_length=1, max_length=200)
    content: Any
    tool_use_id: str | None = Field(default=None, max_length=200)


class VerifyBody(DigestBody):
    expected_digest: str = Field(..., min_length=64, max_length=128)


@router.post(
    "/tool-result-digest",
    openapi_extra={"security": []},
    summary="Canonical SHA-256 digest of a tool result — free.",
    description=DESCRIPTION,
    tags=["integrity", "free", "mcp"],
)
async def tool_result_digest(body: DigestBody):
    try:
        return digest_tool_result(
            body.tool_name,
            body.content,
            tool_use_id=body.tool_use_id,
        )
    except ValueError as exc:
        reason = str(exc)
        status = 413 if reason == "content_too_large" else 400
        return JSONResponse({"error": {"reason": reason}}, status_code=status)


@router.post(
    "/tool-result-verify",
    openapi_extra={"security": []},
    summary="Check a digest against a tool result — free.",
    description=(
        "Recomputes the canonical digest and returns match=true/false. "
        "Use after a paid tool call to confirm the payload before release."
    ),
    tags=["integrity", "free", "mcp"],
)
async def tool_result_verify(body: VerifyBody):
    try:
        return verify_tool_result_digest(
            body.expected_digest,
            body.tool_name,
            body.content,
            tool_use_id=body.tool_use_id,
        )
    except ValueError as exc:
        reason = str(exc)
        status = 413 if reason == "content_too_large" else 400
        return JSONResponse({"error": {"reason": reason}}, status_code=status)


@router.get(
    "/tool-result-digest/sample",
    openapi_extra={"security": []},
    summary="Run POST /tool-result-digest on a fixed sample — free.",
    tags=["integrity", "free", "sample"],
)
async def tool_result_digest_sample():
    out = digest_tool_result(
        SAMPLE["tool_name"],
        SAMPLE["content"],
        tool_use_id=SAMPLE["tool_use_id"],
    )
    return {**SAMPLE, **out, "sample": True}
