"""Category router and specialized free-source clients for POST /search v2.

Phase 0 (2026-09-27) vetted six candidate free sources for commercial
resale use. Two failed the bar outright: OSM Nominatim and Overpass both
explicitly tell "API resellers" to self-host rather than use the public
instance, so neither is used here - the "place" category has no
specialized source and is routed through SearXNG exactly like "general".
The remaining four (GitHub Search, Wikipedia/Wikimedia REST, Hacker News/
Algolia, GDELT) explicitly permit commercial use; Stack Exchange also
passed but has no natural slot in the seven categories below, so it is
not wired in. GitHub Search's own docs put its unauthenticated rate limit
at 10 requests/minute (stricter than the general API) - the WSL
`agentindexworld` token is not a dedicated read-only token, so this stays
unauthenticated rather than embedding a token with unrelated repo
permissions inside a public-facing search path.
"""

import asyncio
import re
import time
from typing import Any

import httpx

from app.handlers import crypto as crypto_handler
from app.handlers import weather as weather_handler
from app.upstream.jev import ask_jev

USER_AGENT = "AgentIndex-x402/1.0 (+https://x402.agentindex.world; contact: comallagency@gmail.com)"
_TIMEOUT = 12.0

ROUTER_CATEGORIES = ["code", "place", "fact", "news", "price", "weather", "general"]
_SECOND_CHOICE_THRESHOLD = 0.6

_ROUTER_INSTRUCTIONS = "Which single category best describes what this search query is asking for?"
_ROUTER_CRITERIA = {
    "code": "A software/programming question: a library, framework, tutorial, code example, or GitHub repository.",
    "place": "A question about a physical place, business, address, or location - restaurants, landmarks, directions.",
    "fact": "A general-knowledge or how/why/what-is question best answered by an encyclopedia article.",
    "news": "A question about a recent or current event, sports result, or product announcement.",
    "price": "A question about the current price or value of a cryptocurrency.",
    "weather": "A question about current or forecast weather conditions for a place.",
    "general": "None of the above - a broad web query needing a general search engine.",
}


async def classify_query(query: str) -> dict[str, float]:
    """One Jev Choice call, returns {category: probability} over ROUTER_CATEGORIES."""
    questions = {"category": {"type": "choice", "instructions": _ROUTER_INSTRUCTIONS, "criteria": _ROUTER_CRITERIA}}
    data = await ask_jev(query, questions)
    return data["answers"]["category"]["probabilities"]


def ranked_categories(probabilities: dict[str, float]) -> list[tuple[str, float]]:
    return sorted(probabilities.items(), key=lambda kv: kv[1], reverse=True)


# --- tiny process-wide rate limiters (GitHub: 10/min unauthenticated docs
# limit; GDELT: "one every 5 seconds" enforced by their edge, per their own
# 429 response body) - a simple min-interval gate, not a real queue, since
# /search's own traffic is nowhere near saturating either yet. -------------

class _MinIntervalGate:
    def __init__(self, min_interval_seconds: float):
        self._min_interval = min_interval_seconds
        self._lock = asyncio.Lock()
        self._last_call = 0.0

    async def wait(self):
        async with self._lock:
            now = time.monotonic()
            delay = self._min_interval - (now - self._last_call)
            if delay > 0:
                await asyncio.sleep(delay)
            self._last_call = time.monotonic()


_github_gate = _MinIntervalGate(6.5)  # 10/min = 6s apart minimum, plus margin
_gdelt_gate = _MinIntervalGate(5.5)   # GDELT's own stated "one every 5 seconds"


# --- 10-minute TTL cache, keyed by (source, normalized query) -------------

_CACHE_TTL_SECONDS = 600
_cache: dict[tuple[str, str], tuple[float, list[dict[str, Any]]]] = {}


def _cache_key(source: str, query: str) -> tuple[str, str]:
    return (source, re.sub(r"\s+", " ", query.strip().lower()))


def _cache_get(source: str, query: str) -> list[dict[str, Any]] | None:
    entry = _cache.get(_cache_key(source, query))
    if not entry:
        return None
    stored_at, results = entry
    if time.monotonic() - stored_at > _CACHE_TTL_SECONDS:
        return None
    return results


def _cache_set(source: str, query: str, results: list[dict[str, Any]]) -> None:
    _cache[_cache_key(source, query)] = (time.monotonic(), results)


def _shape(title, url, extract, date, source, attribution=None) -> dict[str, Any]:
    result = {"title": title, "url": url, "extract": extract, "date": date, "source": source}
    if attribution:
        result["attribution"] = attribution
    return result


# --- code: GitHub repository search ---------------------------------------

async def search_github(query: str, limit: int = 8) -> list[dict[str, Any]]:
    cached = _cache_get("github", query)
    if cached is not None:
        return cached
    await _github_gate.wait()
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, headers={"User-Agent": USER_AGENT}) as client:
            resp = await client.get(
                "https://api.github.com/search/repositories",
                params={"q": query, "sort": "stars", "order": "desc", "per_page": limit},
            )
        if resp.status_code != 200:
            return []
        items = resp.json().get("items") or []
    except httpx.HTTPError:
        return []

    results = [
        _shape(
            item.get("full_name"),
            item.get("html_url"),
            item.get("description"),
            item.get("pushed_at"),
            "github",
        )
        for item in items
        if item.get("html_url")
    ]
    _cache_set("github", query, results)
    return results


# --- fact: Wikipedia/Wikimedia REST search --------------------------------

_QUESTION_FILLER = {
    "how", "does", "do", "did", "what", "is", "are", "was", "were", "the",
    "a", "an", "of", "to", "in", "on", "for", "work", "works", "latest",
}


def _strip_filler(query: str) -> str:
    """Wikipedia's own search ranks noticeably worse on full question
    phrasing than on the bare subject - measured live: 'how does
    photosynthesis work' misses the Photosynthesis article in its top 5,
    'photosynthesis' alone ranks it first."""
    tokens = [t for t in re.findall(r"[\w'-]+", query) if t.lower() not in _QUESTION_FILLER]
    return " ".join(tokens) or query


async def search_wikipedia(query: str, limit: int = 5) -> list[dict[str, Any]]:
    cached = _cache_get("wikipedia", query)
    if cached is not None:
        return cached
    stripped = _strip_filler(query)
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, headers={"User-Agent": USER_AGENT}) as client:
            resp = await client.get(
                "https://en.wikipedia.org/w/rest.php/v1/search/page",
                params={"q": stripped, "limit": limit},
            )
        if resp.status_code != 200:
            return []
        pages = resp.json().get("pages") or []
    except httpx.HTTPError:
        return []

    results = []
    for page in pages:
        title = page.get("title")
        if not title:
            continue
        url = f"https://en.wikipedia.org/wiki/{title.replace(' ', '_')}"
        excerpt = re.sub(r"<[^>]+>", "", page.get("excerpt") or "")
        results.append(
            _shape(
                title, url, excerpt or None, None, "wikipedia",
                attribution="Text available under CC BY-SA 4.0, via Wikipedia contributors.",
            )
        )
    _cache_set("wikipedia", query, results)
    return results


# --- news: GDELT ------------------------------------------------------------
# GDELT's own ToS explicitly allows commercial resale, but its public doc.
# API was measured (2026-09-27) to be highly inconsistent live: sometimes a
# clean ~1s response, sometimes a 429, sometimes a hang past 12s before
# failing outright. A short, GDELT-specific timeout keeps a bad GDELT moment
# from ever costing /search more than a few seconds - it just falls through
# to the SearXNG general path instead, same as any other empty specialized
# source.
_GDELT_TIMEOUT = 5.0


async def search_gdelt(query: str, limit: int = 8) -> list[dict[str, Any]]:
    cached = _cache_get("gdelt", query)
    if cached is not None:
        return cached
    await _gdelt_gate.wait()
    try:
        async with httpx.AsyncClient(timeout=_GDELT_TIMEOUT, headers={"User-Agent": USER_AGENT}) as client:
            resp = await client.get(
                "https://api.gdeltproject.org/api/v2/doc/doc",
                params={
                    "query": query,
                    "mode": "artlist",
                    "format": "json",
                    "maxrecords": limit,
                    "sort": "hybridrel",
                },
            )
        if resp.status_code != 200:
            return []
        articles = resp.json().get("articles") or []
    except (httpx.HTTPError, ValueError):
        return []

    results = [
        _shape(
            a.get("title"), a.get("url"), a.get("domain"),
            a.get("seendate"), "gdelt",
        )
        for a in articles
        if a.get("url")
    ]
    _cache_set("gdelt", query, results)
    return results


# --- price / weather: reuse the existing paid routes' own lookup functions -

_COIN_NAMES = {
    "bitcoin": "bitcoin", "btc": "bitcoin",
    "ethereum": "ethereum", "eth": "ethereum", "ether": "ethereum",
    "solana": "solana", "sol": "solana",
    "cardano": "cardano", "ada": "cardano",
    "dogecoin": "dogecoin", "doge": "dogecoin",
    "ripple": "ripple", "xrp": "ripple",
    "litecoin": "litecoin", "ltc": "litecoin",
    "polkadot": "polkadot", "dot": "polkadot",
    "chainlink": "chainlink", "link": "chainlink",
    "avalanche": "avalanche-2", "avax": "avalanche-2",
    "cosmos": "cosmos", "atom": "cosmos",
    "near": "near",
    "arbitrum": "arbitrum", "arb": "arbitrum",
    "optimism": "optimism", "op": "optimism",
    "uniswap": "uniswap", "uni": "uniswap",
    "tether": "tether", "usdt": "tether",
    "pepe": "pepe",
    "binancecoin": "binancecoin", "bnb": "binancecoin",
}


def _extract_coin(query: str) -> str | None:
    tokens = re.findall(r"[a-z]+", query.lower())
    for token in tokens:
        if token in _COIN_NAMES:
            return _COIN_NAMES[token]
    return None


async def search_price(query: str) -> list[dict[str, Any]]:
    coin = _extract_coin(query)
    if not coin:
        return []
    cached = _cache_get("price", coin)
    if cached is not None:
        return cached
    try:
        data = await crypto_handler._lookup({"coins": [coin], "vs_currency": "usd"})
    except crypto_handler.CryptoError:
        return []
    results = [
        _shape(
            f"{p['id'].capitalize()} price (USD)",
            f"https://www.coingecko.com/en/coins/{p['id']}",
            f"{p['id'].capitalize()} is ${p['price']} USD, {p['change_24h_pct']}% in the last 24h.",
            None, "crypto-live",
        )
        for p in data.get("prices") or []
    ]
    _cache_set("price", coin, results)
    return results


_WEATHER_FILLER = {
    "weather", "meteo", "météo", "forecast", "temperature", "in", "à", "a",
    "for", "pour", "today", "aujourd'hui", "aujourdhui", "now", "tomorrow",
    "demain", "the",
}


def _extract_city(query: str) -> str | None:
    tokens = [t for t in re.findall(r"[\w'-]+", query) if t.lower() not in _WEATHER_FILLER]
    city = " ".join(tokens).strip()
    return city or None


async def search_weather(query: str) -> list[dict[str, Any]]:
    city = _extract_city(query)
    if not city:
        return []
    cached = _cache_get("weather", city)
    if cached is not None:
        return cached
    try:
        data = await weather_handler._lookup({"city": city})
    except weather_handler.WeatherError:
        return []
    cur = data.get("current") or {}
    loc = data.get("location") or {}
    name = loc.get("name") or city
    extract = (
        f"Currently {cur.get('conditions')}, {cur.get('temperature_c')}°C, "
        f"humidity {cur.get('humidity_pct')}%, wind {cur.get('wind_speed_kmh')} km/h."
    )
    results = [
        _shape(
            f"Weather in {name}",
            f"https://open-meteo.com/en/docs#latitude={loc.get('latitude')}&longitude={loc.get('longitude')}",
            extract, cur.get("time"), "weather-live",
        )
    ]
    _cache_set("weather", city, results)
    return results


SOURCE_FOR_CATEGORY = {
    "code": search_github,
    "fact": search_wikipedia,
    "news": search_gdelt,
    "price": search_price,
    "weather": search_weather,
}


async def search_by_category(category: str, query: str, limit: int) -> list[dict[str, Any]]:
    """place and general have no specialized source (see module docstring)
    - callers should route those straight to SearXNG."""
    fn = SOURCE_FOR_CATEGORY.get(category)
    if fn is None:
        return []
    if fn in (search_github, search_wikipedia, search_gdelt):
        return await fn(query, limit)
    return await fn(query)
