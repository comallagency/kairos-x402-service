import base64
import io
import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
import pymupdf
import pymupdf4llm
from pypdf import PdfReader

from app import config, db
from app.receipts import Timer, effective_price, extract_payer_address, make_receipt, price_float
from app.upstream.tokencount import count_tokens
from app.upstream.webfetch import FetchError, fetch_bytes
from app.x402_setup import ROUTE_DESCRIPTIONS

router = APIRouter()

# A real, short, stable, well-known public PDF used only for GET /pdf/sample
# - real fetch and real parse every time, not a canned response (same
# convention as /search/sample calling the real upstream). Was a 1-line
# "Dummy PDF file" test fixture until 2026-09-06 - a buyer evaluating this
# route before paying needs to see it handle an actual document, not three
# words (see cm-central brief).
SAMPLE_PDF_URL = "https://www.ohchr.org/sites/default/files/UDHR/Documents/UDHR_Translations/eng.pdf"

_CALLER_ERROR_REASONS = {
    "missing_url_or_base64", "both_url_and_base64_provided", "invalid_base64",
    "invalid_pdf", "encrypted_pdf", "no_extractable_text", "invalid_url",
}


class PdfError(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _meta_get(meta, attr: str, key: str):
    if meta is None:
        return None
    value = getattr(meta, attr, None)
    if value is None and hasattr(meta, "get"):
        value = meta.get(key)
    return value


def parse_pdf(data: bytes) -> dict:
    """Markdown via pymupdf4llm (headings and tables preserved by its layout
    parser) - text content itself is never generated or altered, only its
    structure is inferred. pypdf still gates validity/encryption and supplies
    metadata, unchanged. A corrupted or encrypted-without-password PDF is an
    explicit error, never partial or fabricated content."""
    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception:
        raise PdfError("invalid_pdf")

    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception:
            raise PdfError("encrypted_pdf")

    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
        try:
            if doc.needs_pass and not doc.authenticate(""):
                raise PdfError("encrypted_pdf")
            markdown = pymupdf4llm.to_markdown(doc)
        finally:
            doc.close()
    except PdfError:
        raise
    except Exception:
        raise PdfError("invalid_pdf")

    if not markdown or not markdown.strip():
        raise PdfError("no_extractable_text")

    meta = reader.metadata
    return {
        "markdown": markdown,
        "metadata": {
            "title": _meta_get(meta, "title", "/Title"),
            "author": _meta_get(meta, "author", "/Author"),
            "pages": len(reader.pages),
            "date": _meta_get(meta, "creation_date", "/CreationDate"),
        },
    }


async def _get_pdf_bytes(body: dict) -> bytes:
    url = body.get("url")
    pdf_base64 = body.get("pdf_base64")
    if url and pdf_base64:
        raise PdfError("both_url_and_base64_provided")
    if url:
        try:
            data, _content_type = await fetch_bytes(url)
        except FetchError as exc:
            raise PdfError(str(exc)[:200])
        return data
    if pdf_base64:
        try:
            return base64.b64decode(pdf_base64, validate=True)
        except Exception:
            raise PdfError("invalid_base64")
    raise PdfError("missing_url_or_base64")


@router.get("/pdf/sample", openapi_extra={"security": []})
async def pdf_sample():
    with Timer() as t:
        try:
            data, _content_type = await fetch_bytes(SAMPLE_PDF_URL)
            parsed = parse_pdf(data)
        except (FetchError, PdfError) as exc:
            return JSONResponse({"error": {"reason": str(exc)[:200]}}, status_code=502)
    token_count = count_tokens(parsed["markdown"])
    receipt = make_receipt(None, "pdf_parse", t.elapsed_ms, 0.0)
    return {**parsed, "token_count": token_count, "x402_receipt": receipt}


@router.post("/pdf", description=ROUTE_DESCRIPTIONS["pdf"])
async def pdf_extract(request: Request):
    payer = extract_payer_address(request)
    user_agent = request.headers.get("user-agent")
    try:
        body = await request.json()
    except Exception:
        body = {}
    body_excerpt = json.dumps(body)[:2000]

    try:
        with Timer() as t:
            data = await _get_pdf_bytes(body)
            parsed = parse_pdf(data)
    except PdfError as exc:
        db.log_request(
            route="pdf", method="POST", status="error", payer=payer,
            user_agent=user_agent, body_excerpt=body_excerpt, error_reason=exc.reason,
        )
        status_code = 400 if exc.reason in _CALLER_ERROR_REASONS else 502
        return JSONResponse({"error": {"reason": exc.reason}}, status_code=status_code)

    token_count = count_tokens(parsed["markdown"])
    price = effective_price(payer, price_float(config.PRICE_PDF))
    db.log_request(
        route="pdf", method="POST", status="paid", latency_ms=t.elapsed_ms,
        amount_usdc=price, payer=payer, user_agent=user_agent, body_excerpt=body_excerpt,
    )
    receipt = make_receipt(None, "pdf_parse", t.elapsed_ms, price)
    return {**parsed, "token_count": token_count, "x402_receipt": receipt}
