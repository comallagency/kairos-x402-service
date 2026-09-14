import re
from typing import Any

import httpx

from app import config

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


async def run_web_search(query: str, max_results: int = 5) -> tuple[list[dict[str, Any]], str | None]:
    """Returns (results, model_served) - model_served is always None here (no
    LLM is involved, see app/handlers/search.py's neutral_model_id() call),
    kept only so this function's signature matches its pre-SearXNG version."""
    max_results = max(1, min(max_results, 10))
    try:
        async with httpx.AsyncClient(timeout=SEARXNG_TIMEOUT_SECONDS) as client:
            resp = await client.get(
                f"{config.SEARXNG_URL}/search",
                params={"q": query, "format": "json"},
            )
            if resp.status_code >= 400:
                raise SearchError(f"upstream_status_{resp.status_code}")
            data = resp.json()
    except httpx.TimeoutException:
        raise SearchError("search_timeout")
    except httpx.RequestError as exc:
        raise SearchError(f"search_failed: {exc}"[:200])

    results = []
    for r in data.get("results") or []:
        url = r.get("url")
        if not url:
            continue
        title = (r.get("title") or "").strip() or None
        results.append(
            {
                "title": title,
                "url": url,
                "extract": _clean_extract(r.get("content")),
                "date": r.get("publishedDate"),
            }
        )
        if len(results) >= max_results:
            break
    return results, None
