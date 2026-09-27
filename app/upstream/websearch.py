import asyncio
import re
from typing import Any

import httpx
from py3langid.langid import MODEL_FILE, LanguageIdentifier

from app import config
from app.upstream.jev import JevError, ask_jev

# Searches now run against a local SearXNG instance (searxng-kairos, same
# docker network as this container - see docker-compose.yml) instead of
# OpenRouter's paid "web" plugin: that plugin was disabled 2026-09-07 when
# the OpenRouter account balance went negative (app/handlers/search.py's
# header comment). SearXNG costs nothing per call and needs no API key, so
# /search no longer depends on OpenRouter account balance at all.
#
# SearXNG's JSON result `content` field is a search-engine snippet, not a
# full scraped page, but it still carries the same boilerplate/duplication
# issues the original OpenRouter "web" plugin extracts had (fragments,
# repeated nav text), so the existing _clean_extract() heuristic is reused
# unchanged.

_MAX_EXTRACT_CHARS = 500
_BOILERPLATE_LINE_MAX_WORDS = 4
_QUERY_STOP_WORDS = {
    "and", "are", "best", "for", "from", "how", "in", "of", "the", "to",
    "what", "with",
}

SEARXNG_TIMEOUT_SECONDS = 15.0


class SearchError(Exception):
    pass


def _clean_extract(raw: str | None) -> str | None:
    """Strip nav-menu-style lines and verbatim-repeated lines from a raw scraped
    page extract, then flatten to one paragraph capped at ~500 chars.

    Heuristic, not site-specific: a line with no sentence-ending punctuation and
    only a handful of words reads as a UI label ("Home", "Open Times") rather
    than prose, so it's dropped; exact-duplicate lines (the same address/phone
    block repeated across a page) are dropped after the first occurrence.
    """
    if not raw:
        return raw

    seen: set[str] = set()
    kept: list[str] = []
    for line in raw.splitlines():
        bare = re.sub(r"^[*\-•]\s*", "", line.strip())
        if not bare:
            continue
        looks_like_sentence = bool(re.search(r"[.!?]\s*$", bare))
        if len(bare.split()) <= _BOILERPLATE_LINE_MAX_WORDS and not looks_like_sentence:
            continue
        key = bare.lower()
        if key in seen:
            continue
        seen.add(key)
        kept.append(bare)

    text = re.sub(r"\s+", " ", " ".join(kept)).strip()

    if len(text) > _MAX_EXTRACT_CHARS:
        cut = text[:_MAX_EXTRACT_CHARS]
        last_space = cut.rfind(" ")
        if last_space > 0:
            cut = cut[:last_space]
        text = cut.rstrip(",;:- ") + "..."

    return text or None


def _shape_result(title, url, extract, date=None) -> dict[str, Any]:
    return {
        "title": (title or "").strip() or None,
        "url": url,
        "extract": _clean_extract(extract),
        "date": date,
    }


def _relevance_score(query: str, result: dict[str, Any]) -> int:
    """Small deterministic re-ranker for noisy public-engine blends. Kept as
    the fallback path in run_web_search() when the Jev rerank call itself
    fails (see _jev_rerank) - a heuristic sort beats no sort at all."""
    tokens = {
        token
        for token in re.findall(r"[a-z0-9]+", query.lower())
        if len(token) >= 3 and token not in _QUERY_STOP_WORDS
    }
    title = (result.get("title") or "").lower()
    url = (result.get("url") or "").lower()
    extract = (result.get("extract") or "").lower()
    score = sum(
        (5 if token in title else 0)
        + (3 if token in url else 0)
        + (1 if token in extract else 0)
        for token in tokens
    )
    phrase = " ".join(re.findall(r"[a-z0-9]+", query.lower()))
    if phrase and phrase in f"{title} {extract}":
        score += 8
    return score


# py3langid is unreliable on short search-query text: measured confidence on
# real queries ranged from 0.09 to 0.37 for unambiguous English (once even
# landing on "qu"/Quechua or "pcm"/Nigerian Pidgin), while a fuller French
# sentence scored 0.98 - so this only trusts a non-English call made with
# real confidence, and never forces "en" explicitly: passing language=en to
# SearXNG was measured to reproducibly return 0 raw results for some queries
# (a SearXNG/bing interaction, not a language-detection problem), which would
# make reliability worse than doing nothing. Leaving English/ambiguous
# queries unrestricted keeps today's stable behavior; a stray off-topic
# result from that (e.g. a French hit on an English query) is now caught by
# the Jev relevance rerank below regardless of why it surfaced.
_LANGUAGE_CONFIDENCE_THRESHOLD = 0.7
_identifier = LanguageIdentifier.from_model_file(MODEL_FILE, norm_probs=True)


def _detect_non_english_language(query: str) -> str | None:
    language, confidence = _identifier.classify(query)
    if language != "en" and confidence >= _LANGUAGE_CONFIDENCE_THRESHOLD:
        return language
    return None


SEARCH_RETRY_DELAY_SECONDS = 1.0
RAW_COLLECTION_SIZE = 20


async def _search_once(query: str, raw_limit: int, language: str | None) -> list[dict[str, Any]]:
    params = {"q": query, "format": "json", "engines": "bing,brave"}
    if language:
        params["language"] = language
    try:
        async with httpx.AsyncClient(timeout=SEARXNG_TIMEOUT_SECONDS) as client:
            resp = await client.get(f"{config.SEARXNG_URL}/search", params=params)
            if resp.status_code >= 400:
                raise SearchError(f"upstream_status_{resp.status_code}")
            data = resp.json()
    except httpx.TimeoutException as exc:
        raise SearchError("search_timeout") from exc
    except httpx.RequestError as exc:
        raise SearchError(f"search_failed: {exc}"[:200]) from exc
    except ValueError as exc:
        raise SearchError("search_invalid_json") from exc

    results = []
    seen_urls: set[str] = set()
    for r in data.get("results") or []:
        url = r.get("url")
        if not url or url in seen_urls:
            continue
        if re.match(r"https?://translate\.google\.", url):
            continue
        seen_urls.add(url)
        results.append(_shape_result(r.get("title"), url, r.get("content"), r.get("publishedDate")))
    return results[:raw_limit]


_SEARCH_RANK_DOC_CHARS = 400
_SEARCH_RANK_INSTRUCTIONS = (
    "Which of these web search results is the best available answer to the "
    "query - an imperfect but genuinely on-topic result beats none of them - "
    "or is every one of them off-topic or about a different subject than "
    "the query?"
)
_NONE_RELEVANT_KEY = "doc_none"
_NONE_RELEVANT_TEXT = (
    "Every result above is off-topic or about a different subject than the "
    "query - none of them, even loosely, addresses what the query is about."
)


async def _jev_rerank(query: str, raw_results: list[dict[str, Any]], max_results: int) -> list[dict[str, Any]]:
    """Reuses the same Jev primitive that powers POST /rank: one Choice
    question whose candidates are the raw results plus a "none of the above"
    sentinel. If the sentinel wins, every candidate was judged irrelevant and
    an empty list is returned - run_web_search() then reports 0 results,
    which app/handlers/search.py already turns into an unbilled 502. If a
    real candidate wins, all candidates are sorted by their probability
    (Jev's own confidence, not a keyword heuristic) and the requested top N
    is returned."""
    criteria = {
        f"doc_{i}": f"{r['title'] or ''} - {r['extract'] or ''}"[:_SEARCH_RANK_DOC_CHARS]
        for i, r in enumerate(raw_results)
    }
    criteria[_NONE_RELEVANT_KEY] = _NONE_RELEVANT_TEXT
    questions = {
        "ranking": {
            "type": "choice",
            "instructions": _SEARCH_RANK_INSTRUCTIONS,
            "criteria": criteria,
        }
    }
    data = await ask_jev(query, questions)
    answer = data["answers"]["ranking"]
    if answer["choice"] == _NONE_RELEVANT_KEY:
        return []
    probabilities = answer["probabilities"]
    order = sorted(
        range(len(raw_results)),
        key=lambda i: probabilities.get(f"doc_{i}", 0.0),
        reverse=True,
    )
    return [raw_results[i] for i in order[:max_results]]


async def run_web_search(query: str, max_results: int = 5) -> tuple[list[dict[str, Any]], str | None]:
    """Returns (results, model_served) - model_served is always None here (no
    LLM is involved, see app/handlers/search.py's neutral_model_id() call),
    kept only so this function's signature matches its pre-SearXNG version.

    Collects a wider raw pool (RAW_COLLECTION_SIZE) from SearXNG than the
    caller asked for, then has Jev pick and rank the genuinely relevant ones
    out of that pool - a zero-hit raw response is retried once, after a
    short pause, before giving up (both configured engines transiently
    missing a query at the same instant is rare but real - this is exactly
    what tripped a live 502 on 2026-09-27), and a 1s gap is enough for that
    kind of blip to clear without meaningfully slowing down a genuine
    no-result query. If the Jev rerank call itself fails (upstream hiccup on
    OpenRouter's alpha decisions endpoint), falls back to the previous
    keyword heuristic rather than failing the whole search over a reranking
    outage."""
    max_results = max(1, min(max_results, 10))
    language = _detect_non_english_language(query)
    raw = await _search_once(query, RAW_COLLECTION_SIZE, language)
    if not raw:
        await asyncio.sleep(SEARCH_RETRY_DELAY_SECONDS)
        raw = await _search_once(query, RAW_COLLECTION_SIZE, language)
    if not raw:
        return [], None

    try:
        results = await _jev_rerank(query, raw, max_results)
    except JevError:
        raw.sort(key=lambda result: _relevance_score(query, result), reverse=True)
        results = raw[:max_results]
    return results, None
