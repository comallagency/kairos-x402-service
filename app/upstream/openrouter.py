import asyncio
import json
import time
from collections import deque
from typing import Any

import httpx

from app import config

CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
KEY_URL = "https://openrouter.ai/api/v1/key"


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


async def get_key_info() -> dict[str, Any]:
    if not config.OPENROUTER_API_KEY:
        raise OpenRouterError("OPENROUTER_API_KEY is not set")
    headers = {"Authorization": f"Bearer {config.OPENROUTER_API_KEY}"}
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(KEY_URL, headers=headers)
        resp.raise_for_status()
        return resp.json()
