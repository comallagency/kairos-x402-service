from fastapi import APIRouter
from fastapi.responses import JSONResponse
from py3langid.langid import MODEL_FILE, LanguageIdentifier

from app.x402_setup import KIT_TAGS

router = APIRouter()

# py3langid's module-level classify() returns an unnormalized log-likelihood
# (can be any negative number, e.g. -243.6) - useless as a "confidence" field.
# from_model_file(..., norm_probs=True) runs the same model but actually
# normalizes across all candidate languages, giving a real 0.0-1.0 value.
_identifier = LanguageIdentifier.from_model_file(MODEL_FILE, norm_probs=True)

DESCRIPTION = (
    "Detect the language of a piece of text - free, no account, no payment. "
    "The entry point to the AgentIndex content kit: once this works, four "
    "paid routes (pdf, web-read, extract, summarize) do the same kind of "
    "work. See GET /capabilities."
)


@router.get(
    "/detect-language",
    openapi_extra={"security": []},
    summary="Detect the language of a piece of text - free.",
    description=DESCRIPTION,
    tags=KIT_TAGS + ["language detection", "free"],
)
async def detect_language(text: str):
    if not text or not text.strip():
        return JSONResponse({"error": {"reason": "missing_text"}}, status_code=400)
    language, confidence = _identifier.classify(text)
    return {"text": text, "language": language, "confidence": round(float(confidence), 4)}


# GET /detect-language/sample - added 2026-09-11.
#
# The other nine kit handlers (search, fact-check, discover, jobs, extract,
# summarize, pdf, web-read, translate) each declare a /sample that replays a
# fixed input, so an agent can see what a route returns before paying for it.
# This one never had it: /detect-language/sample answered 404 from the day the
# route shipped, and the two probes that asked for it (kairos-audit) got a 404
# each. Nothing regressed at the 11:03 rebuild - it was simply never written.
#
# The sample is deliberately a sentence no model is needed to place: short,
# unambiguous, and stable, so that a change in the answer means a change in the
# identifier and not in the input.
SAMPLE_TEXT = "Le rail x402 permet a un agent de payer un autre agent."


@router.get(
    "/detect-language/sample",
    openapi_extra={"security": []},
    summary="Run GET /detect-language on a fixed sample - free.",
    description=(
        "Replays GET /detect-language on a fixed sentence, so an agent can see "
        "the shape of the answer without supplying input. Free, no account, no "
        "payment."
    ),
    tags=KIT_TAGS + ["language detection", "free", "sample"],
)
async def detect_language_sample():
    language, confidence = _identifier.classify(SAMPLE_TEXT)
    return {
        "text": SAMPLE_TEXT,
        "language": language,
        "confidence": round(float(confidence), 4),
        "sample": True,
    }
