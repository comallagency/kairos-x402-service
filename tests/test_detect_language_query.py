import asyncio

from app.handlers.detect_language import detect_language


def test_detect_language_accepts_q_alias() -> None:
    body = asyncio.run(detect_language(text=None, q="hello"))
    assert body["text"] == "hello"
    assert body["language"]
    assert 0.0 <= body["confidence"] <= 1.0


def test_detect_language_missing_params_returns_400() -> None:
    resp = asyncio.run(detect_language(text=None, q=None))
    assert resp.status_code == 400
    assert resp.body  # JSONResponse
