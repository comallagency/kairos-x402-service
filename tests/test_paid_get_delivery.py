"""GET twins must actually deliver after payment (not 405).

Lumière PayCheck and other GET-autopay agents quote GET, pay, then call GET
again. Config-only GET twins returned 402 unpaid and 405 once paid.
"""

import asyncio
from unittest.mock import AsyncMock, patch

from app.handlers.llm_per_model import make_router
from app.handlers.search import router as search_router
from app.handlers.token_risk import router as token_risk_router
from app.handlers.translate import router as translate_router, translate_sample
from app.upstream import websearch


def _methods(router, path: str) -> set[str]:
    found: set[str] = set()
    for route in router.routes:
        if getattr(route, "path", None) == path:
            found.update(route.methods or [])
    return found


def test_search_translate_token_risk_accept_get() -> None:
    assert "GET" in _methods(search_router, "/search")
    assert "POST" in _methods(search_router, "/search")
    assert "GET" in _methods(translate_router, "/translate")
    assert "POST" in _methods(translate_router, "/translate")
    assert "GET" in _methods(token_risk_router, "/token-risk")
    assert "POST" in _methods(token_risk_router, "/token-risk")


def test_llm_per_model_accepts_get() -> None:
    router = make_router(
        route_path="/llm/llama",
        route_key="llm/llama",
        model="meta-llama/llama-4-maverick",
        provider={"only": ["DeepInfra"]},
        description_key="llm-llama",
        sample_request={"messages": [{"role": "user", "content": "Say OK."}], "max_tokens": 8},
        sample_response={"choices": []},
    )
    assert "GET" in _methods(router, "/llm/llama")
    assert "POST" in _methods(router, "/llm/llama")


def test_translate_sample_exposes_model_served_at_top_level() -> None:
    body = asyncio.run(translate_sample())
    assert body["translated_text"] == "Hello world"
    assert "model_served" in body
    assert body["x402_receipt"]["model_served"] == body["model_served"]


def test_jev_rerank_keeps_raw_hits_when_none_relevant() -> None:
    raw = [
        {
            "title": "Lists of earthquakes",
            "url": "https://en.wikipedia.org/wiki/Lists_of_earthquakes",
            "extract": "earthquakes",
            "date": None,
        },
        {
            "title": "Unrelated",
            "url": "https://example.com/other",
            "extract": "cats",
            "date": None,
        },
    ]
    jev_payload = {
        "answers": {
            "ranking": {
                "choice": websearch._NONE_RELEVANT_KEY,
                "probabilities": {
                    "doc_0": 0.1,
                    "doc_1": 0.05,
                    websearch._NONE_RELEVANT_KEY: 0.85,
                },
            }
        }
    }
    with patch("app.upstream.websearch.ask_jev", new=AsyncMock(return_value=jev_payload)):
        ranked = asyncio.run(websearch._jev_rerank("recent earthquake news", raw, 5))
    assert ranked
    assert ranked[0]["url"] == raw[0]["url"]
