"""Client for OpenRouter's Jev Decisions API - typed yes/no (noul),
multiple-choice, and ordinal-scale (score) questions answered with real
probabilities instead of generated text.
See https://openrouter.ai/docs/guides/community/jev.
"""

import httpx

from app import config

DECISIONS_URL = "https://openrouter.ai/api/alpha/decisions"
JEV_MODEL = "typesafe/jev-1.13"


class JevError(Exception):
    pass


async def ask_jev(state, questions: dict, timeout: float = 30.0) -> dict:
    if not config.OPENROUTER_API_KEY:
        raise JevError("OPENROUTER_API_KEY is not set")

    headers = {
        "Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }
    body = {"model": JEV_MODEL, "state": state, "questions": questions}

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(DECISIONS_URL, headers=headers, json=body)
        if resp.status_code >= 400:
            raise JevError(f"Jev error {resp.status_code}: {resp.text[:300]}")
        data = resp.json()

    if "answers" not in data:
        raise JevError(f"Jev response missing 'answers': {str(data)[:300]}")

    return data
