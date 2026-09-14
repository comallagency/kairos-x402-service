import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app import config, db
from app.receipts import Timer, effective_price, extract_payer_address, make_receipt, neutral_model_id, price_float
from app.upstream.openrouter import OpenRouterError, chat_completion, chat_completion_with_fallback
from app.x402_setup import ROUTE_DESCRIPTIONS

router = APIRouter()

MAX_BATCH_SEGMENTS = 200

BATCH_SYSTEM_PROMPT = (
    "You are a translation engine. You will receive a JSON array of text "
    'segments under "segments". Translate EACH segment independently into '
    "the target language. If preserve_format is true, keep markdown syntax, "
    "HTML tags and placeholders like {x} or {{x}} in each segment exactly "
    "unchanged - translate only the natural-language content around them. "
    'Respond with ONLY a JSON object of the form {"detected_source_lang": '
    '"<ISO 639-1 code>", "translations": ["...", ...]}. The translations '
    "array MUST have exactly the same number of elements, in the same "
    "order, as the input segments array - never merge, skip, reorder or "
    "add segments, even if some are empty or very short."
)

SYSTEM_PROMPT = (
    "You are a translation engine. Translate the user's text into the requested "
    "target language. If preserve_format is true, keep markdown syntax, HTML tags "
    'and placeholders like {x} or {{x}} exactly unchanged - translate only the '
    "natural-language content around them. Respond with ONLY a JSON object of the "
    'form {"detected_source_lang": "<ISO 639-1 code>", "translated_text": "..."}, '
    "no extra commentary, no markdown code fences."
)


def _build_messages(
    text: str, target_lang: str, source_lang: str | None, preserve_format: bool
) -> list[dict[str, str]]:
    user_content = json.dumps(
        {
            "text": text,
            "target_lang": target_lang,
            "source_lang": source_lang,
            "preserve_format": preserve_format,
        }
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


def _parse_completion(data: dict) -> tuple[dict, str | None]:
    content = data["choices"][0]["message"]["content"]
    model_served = data.get("model")
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        parsed = {"detected_source_lang": None, "translated_text": content.strip()}
    return parsed, model_served


async def _translate(
    text: str, target_lang: str, source_lang: str | None, preserve_format: bool
) -> tuple[dict, str | None, bool]:
    messages = _build_messages(text, target_lang, source_lang, preserve_format)
    data, used_last_resort = await chat_completion_with_fallback(
        messages, config.OPENROUTER_TRANSLATE_MODELS, config.OPENROUTER_LAST_RESORT_MODEL
    )
    parsed, model_served = _parse_completion(data)
    fallback_used = used_last_resort or (
        model_served is not None and model_served != config.OPENROUTER_TRANSLATE_MODELS[0]
    )
    return parsed, model_served, fallback_used


def _build_batch_messages(
    segments: list[str], target_lang: str, source_lang: str | None, preserve_format: bool
) -> list[dict[str, str]]:
    user_content = json.dumps(
        {
            "segments": segments,
            "target_lang": target_lang,
            "source_lang": source_lang,
            "preserve_format": preserve_format,
        }
    )
    return [
        {"role": "system", "content": BATCH_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


class BatchMisaligned(Exception):
    pass


async def _attempt_batch(messages: list[dict], models: list[str], expected_count: int, max_tokens: int):
    data = await chat_completion(messages, models, max_tokens=max_tokens)
    content = data["choices"][0]["message"]["content"]
    try:
        parsed = json.loads(content)
        translations = parsed["translations"]
        if not isinstance(translations, list) or len(translations) != expected_count:
            raise BatchMisaligned(
                f"expected {expected_count} translations, got "
                f"{len(translations) if isinstance(translations, list) else type(translations).__name__}"
            )
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise BatchMisaligned(f"malformed batch response: {exc}") from exc
    return parsed.get("detected_source_lang"), translations, data.get("model")


async def _translate_batch(
    segments: list[str], target_lang: str, source_lang: str | None, preserve_format: bool
) -> tuple[list[str], str | None, str | None, bool]:
    """Translate all segments in a single upstream call. On a malformed or
    misaligned response from the primary models (a real risk with large,
    strictly-structured JSON output on free-tier models), retries once, whole,
    against the last-resort model rather than silently returning a mismatched
    array - a buyer must never get translations that don't line up with input."""
    messages = _build_batch_messages(segments, target_lang, source_lang, preserve_format)
    # Free-tier chat completions cost us nothing regardless of length; the cap
    # just keeps a single pathological response from running away.
    max_tokens = min(16000, 500 + 60 * len(segments))

    try:
        detected, translations, model_served = await _attempt_batch(
            messages, config.OPENROUTER_TRANSLATE_MODELS, len(segments), max_tokens
        )
        fallback_used = model_served != config.OPENROUTER_TRANSLATE_MODELS[0]
    except (OpenRouterError, BatchMisaligned):
        detected, translations, model_served = await _attempt_batch(
            messages, [config.OPENROUTER_LAST_RESORT_MODEL], len(segments), max_tokens
        )
        fallback_used = True

    return translations, detected, model_served, fallback_used


@router.get("/translate/sample", openapi_extra={"security": []})
async def translate_sample():
    if not config.OPENROUTER_API_KEY:
        return JSONResponse(
            {
                "error": {"reason": "upstream_not_configured"},
                "note": "OPENROUTER_API_KEY is not set in this environment",
            },
            status_code=503,
        )
    try:
        with Timer() as t:
            parsed, model_served, fallback_used = await _translate(
                "Bonjour le monde", "en", None, True
            )
    except OpenRouterError as exc:
        return JSONResponse(
            {"error": {"reason": "upstream_error", "detail": str(exc)[:200]}}, status_code=502
        )
    receipt = make_receipt(neutral_model_id(model_served), "llm", t.elapsed_ms, 0.0, fallback_used=fallback_used)
    return {
        "text": "Bonjour le monde",
        "target_lang": "en",
        "detected_source_lang": parsed.get("detected_source_lang"),
        "translated_text": parsed.get("translated_text"),
        "x402_receipt": receipt,
    }


@router.post("/translate", description=ROUTE_DESCRIPTIONS["translate"])
async def translate(request: Request):
    payer = extract_payer_address(request)
    user_agent = request.headers.get("user-agent")
    try:
        body = await request.json()
    except Exception:
        body = {}
    body_excerpt = json.dumps(body)
    text = body.get("text")
    target_lang = body.get("target_lang")
    source_lang = body.get("source_lang")
    preserve_format = body.get("preserve_format", True)

    if not text or not target_lang:
        db.log_request(
            route="translate", method="POST", status="error", payer=payer,
            user_agent=user_agent, body_excerpt=body_excerpt,
            error_reason="missing_fields",
        )
        return JSONResponse({"error": {"reason": "missing_fields"}}, status_code=400)

    is_batch = isinstance(text, list)
    if is_batch:
        valid = (
            1 <= len(text) <= MAX_BATCH_SEGMENTS
            and all(isinstance(s, str) for s in text)
        )
        if not valid:
            db.log_request(
                route="translate", method="POST", status="error", payer=payer,
                user_agent=user_agent, body_excerpt=body_excerpt,
                error_reason="invalid_batch_text",
            )
            return JSONResponse(
                {
                    "error": {
                        "reason": "invalid_batch_text",
                        "detail": f"text array must have 1-{MAX_BATCH_SEGMENTS} string segments",
                    }
                },
                status_code=400,
            )
    elif not isinstance(text, str):
        db.log_request(
            route="translate", method="POST", status="error", payer=payer,
            user_agent=user_agent, body_excerpt=body_excerpt,
            error_reason="invalid_text_type",
        )
        return JSONResponse({"error": {"reason": "invalid_text_type"}}, status_code=400)

    try:
        with Timer() as t:
            if is_batch:
                translations, detected_source_lang, model_served, fallback_used = await _translate_batch(
                    text, target_lang, source_lang, preserve_format
                )
            else:
                parsed, model_served, fallback_used = await _translate(
                    text, target_lang, source_lang, preserve_format
                )
    except OpenRouterError as exc:
        db.log_request(
            route="translate", method="POST", status="error", payer=payer,
            user_agent=user_agent, body_excerpt=body_excerpt,
            error_reason=str(exc)[:200],
        )
        return JSONResponse(
            {"error": {"reason": "upstream_error", "detail": str(exc)[:200]}}, status_code=502
        )

    price = effective_price(payer, price_float(config.PRICE_TRANSLATE))
    db.log_request(
        route="translate", method="POST", status="paid",
        latency_ms=t.elapsed_ms, amount_usdc=price, payer=payer,
        user_agent=user_agent, body_excerpt=body_excerpt,
    )

    if is_batch:
        receipt = make_receipt(
            neutral_model_id(model_served), "llm", t.elapsed_ms, price,
            fallback_used=fallback_used, segments_processed=len(text),
        )
        return {
            "text": text,
            "target_lang": target_lang,
            "detected_source_lang": detected_source_lang,
            "translated_text": translations,
            "x402_receipt": receipt,
        }

    receipt = make_receipt(neutral_model_id(model_served), "llm", t.elapsed_ms, price, fallback_used=fallback_used)
    return {
        "text": text,
        "target_lang": target_lang,
        "detected_source_lang": parsed.get("detected_source_lang"),
        "translated_text": parsed.get("translated_text"),
        "x402_receipt": receipt,
    }
