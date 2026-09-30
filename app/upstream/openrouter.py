import asyncio
import json
import time
from collections import deque
from typing import Any

import httpx

from app import config

CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
KEY_URL = "https://openrouter.ai/api/v1/key"
CREDITS_URL = "https://openrouter.ai/api/v1/credits"
MODELS_URL = "https://openrouter.ai/api/v1/models"


class OpenRouterError(Exception):
    pass


class RateLimiter:
    """In-process token-bucket limiter. OpenRouter caps this account at
    20 requests/min - shared across /translate and /jobs."""

    def __init__(self, max_calls: int, period_seconds: float):
        self.max_calls = max_calls
        self.period_seconds = period_seconds
        self._calls: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            while self._calls and now - self._calls[0] > self.period_seconds:
                self._calls.popleft()
            if len(self._calls) >= self.max_calls:
                sleep_for = self.period_seconds - (now - self._calls[0])
                if sleep_for > 0:
                    await asyncio.sleep(sleep_for)
            self._calls.append(time.monotonic())


rate_limiter = RateLimiter(max_calls=20, period_seconds=60.0)


async def chat_completion(
    messages: list[dict[str, str]],
    models: list[str],
    max_tokens: int = 1024,
    temperature: float = 0.2,
    timeout: float = 30.0,
    extra_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not config.OPENROUTER_API_KEY:
        raise OpenRouterError("OPENROUTER_API_KEY is not set")

    await rate_limiter.acquire()

    body: dict[str, Any] = {
        "models": models,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "provider": {"data_collection": "deny"},
    }
    if extra_body:
        body.update(extra_body)

    headers = {
        "Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(CHAT_URL, json=body, headers=headers)
        if resp.status_code >= 400:
            raise OpenRouterError(f"OpenRouter error {resp.status_code}: {resp.text[:300]}")
        data = resp.json()

    if "error" in data:
        raise OpenRouterError(f"OpenRouter error: {data['error']}")

    # A 200 with empty/null message.content is a real, observed failure mode
    # (a reasoning model - e.g. minimax-m2.7:free - spends its whole max_tokens
    # budget on hidden reasoning and returns content: None), not an edge case.
    # Treating it as success let a malformed response reach every LLM route's
    # own parsing code as an unhandled crash (summarize.py: AttributeError on
    # None.strip(), found via the Contrôleur's real /summarize/sample probe).
    # Raising here makes chat_completion_with_fallback's existing except
    # OpenRouterError retry with the last-resort model, so every caller is
    # covered without a per-route special case.
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise OpenRouterError(f"OpenRouter response missing choices[0].message.content: {json.dumps(data)[:300]}") from exc
    if not content or not content.strip():
        raise OpenRouterError(f"OpenRouter returned empty content: {json.dumps(data)[:300]}")

    return data


async def chat_completion_with_fallback(
    messages: list[dict[str, str]],
    models: list[str],
    last_resort_model: str,
    **kwargs: Any,
) -> tuple[dict[str, Any], bool]:
    """Try the primary models list (native OpenRouter fallback, max 3 entries),
    then a single last-resort model if all of them failed.

    Returns (response_data, used_last_resort).
    """
    try:
        return await chat_completion(messages, models, **kwargs), False
    except OpenRouterError:
        data = await chat_completion(messages, [last_resort_model], **kwargs)
        return data, True


# --- shared balance check, for routes that pay OpenRouter before delivering ---
#
# Only app/jobs_worker.py's flow needs this today: POST /jobs settles x402
# payment at creation time (app/handlers/jobs.py::create_job), before the
# actual OpenRouter calls happen later, asynchronously - unlike every
# synchronous route (search/translate/summarize/extract/...), which settles
# only after a successful upstream response and so fails closed on its own.
# Any future route with the same "settle before calling OpenRouter" shape
# should reuse this rather than re-implement its own balance check.

_BALANCE_CACHE_TTL_SECONDS = 60.0
_balance_cache: dict[str, float] = {}
_balance_lock = asyncio.Lock()


async def get_balance_usd() -> float:
    """Real OpenRouter balance (total_credits - total_usage), cached for
    _BALANCE_CACHE_TTL_SECONDS to avoid hammering /api/v1/credits on every
    request to a gated route."""
    now = time.monotonic()
    cached = _balance_cache.get("value")
    cached_at = _balance_cache.get("at")
    if cached is not None and cached_at is not None and now - cached_at < _BALANCE_CACHE_TTL_SECONDS:
        return cached

    async with _balance_lock:
        now = time.monotonic()
        cached = _balance_cache.get("value")
        cached_at = _balance_cache.get("at")
        if cached is not None and cached_at is not None and now - cached_at < _BALANCE_CACHE_TTL_SECONDS:
            return cached

        if not config.OPENROUTER_API_KEY:
            raise OpenRouterError("OPENROUTER_API_KEY is not set")
        headers = {"Authorization": f"Bearer {config.OPENROUTER_API_KEY}"}
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(CREDITS_URL, headers=headers)
            resp.raise_for_status()
            data = resp.json()["data"]

        balance = float(data["total_credits"]) - float(data["total_usage"])
        _balance_cache["value"] = balance
        _balance_cache["at"] = now
        return balance


async def has_sufficient_balance(min_usd: float) -> bool:
    """True only if the (cached) balance is confirmed >= min_usd. Fails
    closed - any error reading the balance (network, missing key, OpenRouter
    down) returns False, since the whole point is to avoid charging a buyer
    for a call we can't confirm we can afford."""
    try:
        balance = await get_balance_usd()
    except Exception:
        return False
    return balance >= min_usd


# --- model catalog, for POST /v1/models and the LLM gateway's per-request
# pricing (app/handlers/llm_gateway.py). Cached for _MODELS_CACHE_TTL_SECONDS
# since OpenRouter's catalog barely changes minute to minute and this can be
# read on every /v1/chat/completions price quote.

_MODELS_CACHE_TTL_SECONDS = 3600.0
_models_cache: dict[str, Any] = {}
_models_lock = asyncio.Lock()


async def get_models() -> list[dict[str, Any]]:
    """Raw OpenRouter model catalog, filtered to models with real per-token
    pricing - a handful of models (e.g. typesafe/jev-router) report
    pricing.prompt/completion as "-1" (variable/router pricing that can't be
    quoted upfront), which the gateway can't offer a fixed-formula ceiling
    for, so those are dropped here rather than in every caller."""
    now = time.monotonic()
    cached = _models_cache.get("value")
    cached_at = _models_cache.get("at")
    if cached is not None and cached_at is not None and now - cached_at < _MODELS_CACHE_TTL_SECONDS:
        return cached

    async with _models_lock:
        now = time.monotonic()
        cached = _models_cache.get("value")
        cached_at = _models_cache.get("at")
        if cached is not None and cached_at is not None and now - cached_at < _MODELS_CACHE_TTL_SECONDS:
            return cached

        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(MODELS_URL)
            resp.raise_for_status()
            data = resp.json()["data"]

        models = []
        for m in data:
            pricing = m.get("pricing") or {}
            try:
                prompt_price = float(pricing.get("prompt", -1))
                completion_price = float(pricing.get("completion", -1))
            except (TypeError, ValueError):
                continue
            if prompt_price < 0 or completion_price < 0:
                continue
            models.append(m)

        _models_cache["value"] = models
        _models_cache["at"] = now
        return models


async def chat_completion_raw(
    model: str,
    messages: list[dict[str, str]],
    max_tokens: int,
    timeout: float = 60.0,
    provider: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Single-model call for the LLM gateway (app/handlers/llm_gateway.py)
    and Pack 2's 5 pinned per-model routes (app/handlers/llm_per_model.py,
    2026-09-30) - deliberately separate from chat_completion(): the caller
    passes through exactly the model (and, for Pack 2, exact provider) the
    buyer paid for (no multi-model fallback list, no opinionated "empty
    content is an error" retry-worthy failure - the buyer sees the real
    upstream response either way), and always requests usage:{include:true}
    so the real per-call cost is available to compute the "upto" settlement
    amount (llm_gateway.py) or for margin logging (Pack 2, "exact" only).

    provider: optional pin, e.g. {"only": ["Groq"], "allow_fallbacks": False}
    - merged with the existing data_collection:deny default rather than
    replacing it, so a pin never silently drops that privacy setting."""
    if not config.OPENROUTER_API_KEY:
        raise OpenRouterError("OPENROUTER_API_KEY is not set")

    await rate_limiter.acquire()

    body_provider: dict[str, Any] = {"data_collection": "deny"}
    if provider:
        body_provider.update(provider)

    body = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "usage": {"include": True},
        "provider": body_provider,
    }
    headers = {
        "Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(CHAT_URL, json=body, headers=headers)
        if resp.status_code >= 400:
            raise OpenRouterError(f"OpenRouter error {resp.status_code}: {resp.text[:300]}")
        data = resp.json()

    if "error" in data:
        raise OpenRouterError(f"OpenRouter error: {data['error']}")

    return data


async def get_key_info() -> dict[str, Any]:
    if not config.OPENROUTER_API_KEY:
        raise OpenRouterError("OPENROUTER_API_KEY is not set")
    headers = {"Authorization": f"Bearer {config.OPENROUTER_API_KEY}"}
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(KEY_URL, headers=headers)
        resp.raise_for_status()
        return resp.json()
