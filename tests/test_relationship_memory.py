from fastapi.testclient import TestClient

from app.main import app
from app.relationship_memory import SAMPLE_CARD, validate_card

client = TestClient(app)


def test_sample_valid():
    normalized, errors = validate_card(SAMPLE_CARD)
    assert errors == []
    assert normalized is not None
    assert normalized["who"] == SAMPLE_CARD["who"]


def test_missing_who():
    card = {**SAMPLE_CARD, "who": ""}
    _, errors = validate_card(card)
    assert any(e["path"] == "who" for e in errors)


def test_unknown_field():
    card = {**SAMPLE_CARD, "extra": 1}
    _, errors = validate_card(card)
    assert any(e["reason"] == "unknown_field" for e in errors)


def test_post_relationship_memory_canonical_path():
    body = {
        "card": {
            "v": 1,
            "who": "peer-demo",
            "channel": "http",
            "first_seen_at": "2026-09-18T14:00:00+00:00",
            "exchanges": 1,
        }
    }
    for path in ("/relationship-memory", "/relationship-memory/validate"):
        r = client.post(path, json=body)
        assert r.status_code == 200
        data = r.json()
        assert data["valid"] is True
        assert data["normalized"]["who"] == "peer-demo"


def test_cemetery_rule():
    card = {
        **SAMPLE_CARD,
        "unanswered_since": "2026-09-10T00:00:00+00:00",
        "answered_at": "2026-09-18T00:00:00+00:00",
    }
    _, errors = validate_card(card)
    assert any(e["reason"] == "answered_after_unanswered_marker" for e in errors)
