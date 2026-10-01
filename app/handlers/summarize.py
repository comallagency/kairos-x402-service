import json
import re

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app import config, db
from app.receipts import Timer, effective_price, extract_payer_address, make_receipt, neutral_model_id, price_float
from app.upstream.openrouter import OpenRouterError, chat_completion_with_fallback
from app.upstream.webfetch import FetchError, extract_markdown, fetch_html
from app.x402_setup import ROUTE_DESCRIPTIONS

router = APIRouter()

# 100 -> 180 (2026-09-25): today's working free-tier model spends part of
# its budget on hidden reasoning before any visible output - 100 tokens
# produced empty content even from the best available model, see
# config.OPENROUTER_TRANSLATE_MODELS.
_LENGTH_TARGETS = {
    "short": ("2-3 sentences", 180),
    "medium": ("one paragraph (roughly 100-150 words)", 250),
    "long": ("3-4 paragraphs", 700),
}

SYSTEM_PROMPT_TEMPLATE = (
    "Summarize the following content, neutral and factual. Do not invent "
    "facts beyond what is given. Target length: {length_desc}. Output ONLY "
    "the summary itself - no preamble, no meta-commentary such as 'Here is "
    "a summary' or 'Sure, here's...', no closing remarks."
)

# Belt-and-suspenders cleanup (2026-09-30): the prompt instruction above
# reduces but does not eliminate preamble leakage from smaller free-tier
# models - caught live in the delivery audit ("Here is a 2-3 sentence
# summary of the content:" prefixing a correct, otherwise-clean summary).
# Strips a single leading line that announces the summary rather than
# being part of it - deliberately narrow (anchored to the start of the
# string, common announcement verbs only) so it can never eat into a
# summary that legitimately starts with one of these words as its own
# first sentence.
_PREAMBLE_RE = re.compile(
    r"^(?:here'?s|here is|sure,?|certainly,?|of course,?)\b.*:\s*\n*",
    re.IGNORECASE,
)


def _strip_preamble(summary: str) -> str:
    cleaned = _PREAMBLE_RE.sub("", summary, count=1).strip()
    return cleaned or summary

SAMPLE_TEXT = (
    "Photosynthesis is the process by which green plants, algae, and some "
    "bacteria convert light energy, usually from the sun, into chemical "
    "energy stored in glucose. This process occurs in chloroplasts and uses "
    "carbon dioxide and water as inputs, releasing oxygen as a byproduct."
)

_CALLER_ERROR_REASONS = {"provide_exactly_one_of_url_text_html", "no_extractable_content"}


class SummarizeError(Exception):
    def __init__(self, reason: str, detail: str | None = None):
        self.reason = reason
        self.detail = detail
        super().__init__(reason)


async def _get_content_and_sources(body: dict) -> tuple[str, list[str]]:
    url = body.get("url")
    text = body.get("text")
    html = body.get("html")
    provided = [v for v in (url, text, html) if v]
    if len(provided) != 1:
        raise SummarizeError("provide_exactly_one_of_url_text_html")
    if url:
        try:
            fetched_html = await fetch_html(url)
        except FetchError as exc:
            raise SummarizeError("fetch_failed", str(exc)[:200])
        markdown = extract_markdown(fetched_html, url=url)
        if not markdown:
            raise SummarizeError("no_extractable_content")
        return markdown, [url]
    if html:
        markdown = extract_markdown(html, url=None)
        if not markdown:
            raise SummarizeError("no_extractable_content")
        return markdown, []
    return text, []


async def _summarize_content(content: str, length: str) -> tuple[str, str | None, bool]:
    length_desc, max_tokens = _LENGTH_TARGETS.get(length, _LENGTH_TARGETS["medium"])
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT_TEMPLATE.format(length_desc=length_desc)},
        {"role": "user", "content": content[:12000]},
    ]
    try:
        data, used_last_resort = await chat_completion_with_fallback(
            messages, config.OPENROUTER_TRANSLATE_MODELS, config.OPENROUTER_LAST_RESORT_MODEL, max_tokens=max_tokens,
        )
    except OpenRouterError as exc:
        raise SummarizeError("upstream_error", str(exc)[:200])
    summary = _strip_preamble(data["choices"][0]["message"]["content"].strip())
    model_served = data.get("model")
    fallback_used = used_last_resort or (model_served is not None and model_served != config.OPENROUTER_TRANSLATE_MODELS[0])
    return summary, model_served, fallback_used


@router.get("/summarize/sample", openapi_extra={"security": []})
async def summarize_sample():
    # Static capture (2026-10-01), not a live call: this was making a real
    # OpenRouter summary on every anonymous hit, free - found during the
    # gemini-flash/deepseek leak audit (same bug class, see CLAUDE.md).
    return {
        "summary": "The article explains how photosynthesis converts light energy into chemical energy in plants.",
        "length": "short",
        "sources": ["https://en.wikipedia.org/wiki/Photosynthesis"],
        "x402_receipt": make_receipt(None, "llm", 1, 0.0),
    }


@router.post("/summarize", description=ROUTE_DESCRIPTIONS["summarize"])
async def summarize(request: Request):
    payer = extract_payer_address(request)
    user_agent = request.headers.get("user-agent")
    try:
        body = await request.json()
    except Exception:
        body = {}
    body_excerpt = json.dumps(body)[:2000]
    length = body.get("length", "medium")
    if length not in _LENGTH_TARGETS:
        length = "medium"

    try:
        with Timer() as t:
            content, sources = await _get_content_and_sources(body)
            summary, model_served, fallback_used = await _summarize_content(content, length)
    except SummarizeError as exc:
        db.log_request(
            route="summarize", method="POST", status="error", payer=payer,
            user_agent=user_agent, body_excerpt=body_excerpt, error_reason=exc.reason,
        )
        status_code = 400 if exc.reason in _CALLER_ERROR_REASONS else 502
        return JSONResponse({"error": {"reason": exc.reason, "detail": exc.detail}}, status_code=status_code)

    price = effective_price(payer, price_float(config.PRICE_SUMMARIZE))
    db.log_request(
        route="summarize", method="POST", status="paid", latency_ms=t.elapsed_ms,
        amount_usdc=price, payer=payer, user_agent=user_agent, body_excerpt=body_excerpt,
    )
    receipt = make_receipt(neutral_model_id(model_served), "llm", t.elapsed_ms, price, fallback_used=fallback_used)
    return {"summary": summary, "length": length, "sources": sources, "x402_receipt": receipt}
