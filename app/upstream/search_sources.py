"""Category router and specialized free-source clients for POST /search v2.

Phase 0 (2026-09-27) vetted six candidate free sources for commercial
resale use. Two failed the bar outright: OSM Nominatim and Overpass both
explicitly tell "API resellers" to self-host rather than use the public
instance, so neither is used here - "place" falls back to Wikivoyage
(same Wikimedia Foundation ToS and CC BY-SA 4.0 license as Wikipedia,
confirmed 2026-09-28) before SearXNG. The remaining sources (GitHub
Search, Wikipedia/Wikivoyage REST, GDELT) explicitly permit commercial
use; Stack Exchange and Hacker News also passed but have no natural slot
in the seven categories below, so neither is wired in. GitHub Search's
own docs put its unauthenticated rate limit at 10 requests/minute
(stricter than the general API) - config.GITHUB_SEARCH_TOKEN, when set,
raises this via the search's own per-token authenticated quota; the WSL
`agentindexworld` token is not a dedicated read-only token, so that one
specifically is never used here.

"news" (2026-09-28, v2.1): GDELT's public API is real but was measured to
be highly inconsistent live (see search_gdelt's own comment). Wikipedia
is a reliable second attempt for a recent-but-now-settled fact (a
release date, a race result once it has its own or an updated page) -
it will not have same-day breaking news, but it usually catches up
faster than SearXNG's blended engines get a clean hit for this kind of
phrasing. SearXNG remains the final fallback either way.

"racing sources" (2026-09-28, v2.2): every category's specialized
source(s) now run under CATEGORY_SOURCES via _race() - each with its own
timeout, none blocking the others, the slowest simply contributing
nothing if it doesn't answer in time (see _race). Added Hacker News
(Algolia's public hn.algolia.com search API, already vetted for
commercial use in the Phase 0 review above, just never wired in before)
as a third "news" source alongside GDELT and Wikipedia - HN's own content
is tech/startup news, a good match for tech-news queries specifically.

Real contention found and fixed the same day: two DIFFERENT concurrent
search_github calls measured at 7.165s combined vs 1.892s for one call
alone - not network variance, _github_gate's old wait() blocked the
second call behind the first's minimum-interval lock, serializing any
two concurrent queries that both needed GitHub (same root cause would hit
_gdelt_gate for two concurrent news queries). Fixed by making both gates
non-blocking (try_acquire): a call that can't get the slot right now
returns empty immediately rather than waiting - the true rate limit is
still respected (the gate still enforces the same minimum interval
between calls that DO go through), but a caller under concurrent load
degrades gracefully instead of paying someone else's wait. Verified
searxng-kairos itself is NOT a comparable bottleneck: 5 concurrent
_search_once calls measured at 1.891s combined, close to linear given the
container runs only one worker process (searxng worker-1) but
GRANIAN_BLOCKING_THREADS=4 - no fix needed there. No shared httpx client
with a connection-pool limit was found anywhere in this module either;
every source function opens its own short-lived client per call.
"""

import asyncio
import re
import time
from typing import Any

import httpx

from app import config
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
    """Non-blocking (2026-09-28, was a blocking wait() - see module
    docstring for the real ~7s contention this caused under two concurrent
    same-source queries). try_acquire() reserves the slot and returns True
    only if the minimum interval has already elapsed; otherwise it returns
    False immediately and reserves nothing, so the caller can treat this
    source as empty for this request rather than pay another request's
    wait. The true rate limit is still enforced - calls that DO get True
    are still at least min_interval_seconds apart - only the "block and
    queue" behavior is gone."""

    def __init__(self, min_interval_seconds: float):
        self._min_interval = min_interval_seconds
        self._lock = asyncio.Lock()
        self._last_call = 0.0

    async def try_acquire(self) -> bool:
        async with self._lock:
            now = time.monotonic()
            if now - self._last_call < self._min_interval:
                return False
            self._last_call = now
            return True


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
# Unauthenticated: 10 req/min. With config.GITHUB_SEARCH_TOKEN (a read-only
# token the operator provides, never the WSL admin token - see module
# docstring), GitHub raises this to the token's own authenticated search
# quota (30 req/min for a classic/fine-grained PAT) - the _github_gate stays
# at the conservative unauthenticated interval either way, since /search's
# own traffic is nowhere near saturating even the lower limit yet.

async def search_github(query: str, limit: int = 8) -> list[dict[str, Any]]:
    cached = _cache_get("github", query)
    if cached is not None:
        return cached
    if not await _github_gate.try_acquire():
        return []  # another call already used this interval's slot - not an error, just empty
    headers = {"User-Agent": USER_AGENT}
    if config.GITHUB_SEARCH_TOKEN:
        headers["Authorization"] = f"Bearer {config.GITHUB_SEARCH_TOKEN}"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, headers=headers) as client:
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


async def _search_wikimedia_project(domain: str, source: str, project_name: str, query: str, limit: int) -> list[dict[str, Any]]:
    cached = _cache_get(source, query)
    if cached is not None:
        return cached
    stripped = _strip_filler(query)
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, headers={"User-Agent": USER_AGENT}) as client:
            resp = await client.get(
                f"https://{domain}/w/rest.php/v1/search/page",
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
        url = f"https://{domain}/wiki/{title.replace(' ', '_')}"
        excerpt = re.sub(r"<[^>]+>", "", page.get("excerpt") or "")
        results.append(
            _shape(
                title, url, excerpt or None, None, source,
                attribution=f"Text available under CC BY-SA 4.0, via {project_name} contributors.",
            )
        )
    _cache_set(source, query, results)
    return results


async def search_wikipedia(query: str, limit: int = 5) -> list[dict[str, Any]]:
    return await _search_wikimedia_project("en.wikipedia.org", "wikipedia", "Wikipedia", query, limit)


# Wikivoyage: same Wikimedia Foundation Terms of Use, same CC BY-SA 4.0
# license as Wikipedia (verified 2026-09-28) - no separate or stricter
# clause found for Wikivoyage specifically. Used for "place" before SearXNG,
# since it actually indexes named destinations/attractions/restaurants by
# article, unlike Nominatim/Overpass (excluded, see module docstring).
async def search_wikivoyage(query: str, limit: int = 5) -> list[dict[str, Any]]:
    return await _search_wikimedia_project("en.wikivoyage.org", "wikivoyage", "Wikivoyage", query, limit)


# --- news: GDELT ------------------------------------------------------------
# GDELT's own ToS explicitly allows commercial resale, but its public doc.
# API was measured (2026-09-27) to be highly inconsistent live: sometimes a
# clean ~1s response, sometimes a 429, sometimes a hang past 12s before
# failing outright. A short, GDELT-specific timeout keeps a bad GDELT moment
# from ever costing /search more than a few seconds - it just falls through
# to the other raced news sources (Wikipedia, Hacker News) or SearXNG
# instead. Tightened 5.0 -> 2.5 on 2026-09-28 to fit the tighter overall
# per-source race budget (see CATEGORY_SOURCES / _race).
_GDELT_TIMEOUT = 2.5


async def search_gdelt(query: str, limit: int = 8) -> list[dict[str, Any]]:
    cached = _cache_get("gdelt", query)
    if cached is not None:
        return cached
    if not await _gdelt_gate.try_acquire():
        return []  # another call already used this interval's slot - not an error, just empty
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


# --- news (tech): Hacker News via Algolia's public search API -------------
# hn.algolia.com/api/v1/search - free, no key, already vetted for
# commercial use in the Phase 0 review (module docstring), just never wired
# in before 2026-09-28. HN's own content is tech/startup news, a natural
# complement to GDELT+Wikipedia for tech-flavored news queries.
_HN_TIMEOUT = 2.0


async def search_hackernews(query: str, limit: int = 6) -> list[dict[str, Any]]:
    cached = _cache_get("hackernews", query)
    if cached is not None:
        return cached
    try:
        async with httpx.AsyncClient(timeout=_HN_TIMEOUT, headers={"User-Agent": USER_AGENT}) as client:
            resp = await client.get(
                "https://hn.algolia.com/api/v1/search",
                params={"query": query, "tags": "story", "hitsPerPage": limit},
            )
        if resp.status_code != 200:
            return []
        hits = resp.json().get("hits") or []
    except (httpx.HTTPError, ValueError):
        return []

    results = [
        _shape(
            h.get("title"),
            h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID')}",
            None,
            h.get("created_at"),
            "hackernews",
        )
        for h in hits
        if h.get("title")
    ]
    _cache_set("hackernews", query, results)
    return results


_NEWS_PER_SOURCE_LIMIT = 6  # not `limit` - see CATEGORY_SOURCES/_race docstrings


def _dedup_merge(result_lists: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    merged: list[dict[str, Any]] = []
    for results in result_lists:
        for r in results:
            url = r.get("url")
            if not url or url in seen:
                continue
            seen.add(url)
            merged.append(r)
    return merged


async def _race(sources: list[tuple[str, float, Any]]) -> list[dict[str, Any]]:
    """Runs every (name, timeout_s, coroutine) concurrently - a real race,
    not "everyone gets their own timeout but we still wait for the slowest
    one regardless" (2026-09-28: that was the first version of this
    function, and it was NOT actually faster for "news" in practice - GDELT
    at 2.5s made every news search take ~2.5s even when Wikipedia and
    Hacker News (both capped at 2.0s) had already answered, because a plain
    gather over individually-wait_for-wrapped coroutines still blocks on
    ALL of them before returning, fast ones included. Found by direct
    measurement: collect_raw for a news query took 3.06s despite no single
    source being allowed to exceed 2.5s).

    Two-tier wait instead: first wait only up to the SHORTEST timeout in
    the group. If that already produced any results, return them
    immediately and cancel whatever's still running (2026-09-28: "the
    slowest never blocks the others" now means what it says). Only if nothing
    useful came back in that fast tier do the remaining sources get the
    rest of their own budget - e.g. GDELT specifically still gets its full
    2.5s, but only when Wikipedia/HN (2.0s) came back empty first."""
    if not sources:
        return []
    tasks = {name: asyncio.ensure_future(coro) for name, _timeout_s, coro in sources}
    timeouts = {name: timeout_s for name, timeout_s, _coro in sources}
    shortest = min(timeouts.values())
    longest = max(timeouts.values())

    done, pending = await asyncio.wait(tasks.values(), timeout=shortest)

    def _results_from(task_set) -> list[list[dict[str, Any]]]:
        out = []
        for task in task_set:
            if task.cancelled() or task.exception() is not None:
                continue
            out.append(task.result())
        return out

    early = _dedup_merge(_results_from(done))
    if early or shortest >= longest or not pending:
        for task in pending:
            task.cancel()
        return early

    # Fast tier came back empty - give the slower source(s) their remaining budget.
    done2, pending2 = await asyncio.wait(pending, timeout=longest - shortest)
    for task in pending2:
        task.cancel()
    return _dedup_merge(_results_from(done | done2))


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


async def search_price(query: str, limit: int = 1) -> list[dict[str, Any]]:
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


async def search_weather(query: str, limit: int = 1) -> list[dict[str, Any]]:
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


# Each category lists the sources that race for it: (name, timeout_s, fn).
# "news" is the only multi-source category (GDELT + Wikipedia + Hacker
# News, added 2026-09-28) - every other category keeps its original single
# source, now wrapped in the same _race mechanism so it can never exceed
# its own budget either, for one consistent code path.
CATEGORY_SOURCES: dict[str, list[tuple[str, float, Any]]] = {
    "code": [("github", 2.0, search_github)],
    "place": [("wikivoyage", 2.0, search_wikivoyage)],
    "fact": [("wikipedia", 2.0, search_wikipedia)],
    "news": [
        ("gdelt", _GDELT_TIMEOUT, search_gdelt),
        ("wikipedia", 2.0, search_wikipedia),
        ("hackernews", _HN_TIMEOUT, search_hackernews),
    ],
    "price": [("price", 2.0, search_price)],
    "weather": [("weather", 2.0, search_weather)],
}


async def search_by_category(category: str, query: str, limit: int) -> list[dict[str, Any]]:
    """"general" has no specialized source - callers should route it
    straight to SearXNG. Every source function takes the same (query,
    limit) signature. "news" caps each source at _NEWS_PER_SOURCE_LIMIT
    regardless of the caller's `limit` (the overall raw-collection budget) -
    measured live that requesting a full 20 Wikipedia hits for "who won the
    last F1 race" pulls in enough tangential same-keyword pages (a film
    titled F1, a video game, a driver's biography, one specific Grand Prix)
    that the final Jev rerank starts correctly judging the *diluted* pool
    as not a direct answer and discarding everything, whereas the same
    source capped at 6 keeps only the closest hits and reliably survives
    rerank."""
    sources = CATEGORY_SOURCES.get(category)
    if not sources:
        return []
    per_source_limit = _NEWS_PER_SOURCE_LIMIT if category == "news" else limit
    merged = await _race([(name, timeout_s, fn(query, per_source_limit)) for name, timeout_s, fn in sources])
    return merged[:limit]
