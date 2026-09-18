from app.relationship_memory import SAMPLE_CARD, validate_card


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


def test_cemetery_rule():
    card = {
        **SAMPLE_CARD,
        "unanswered_since": "2026-09-10T00:00:00+00:00",
        "answered_at": "2026-09-18T00:00:00+00:00",
    }
    _, errors = validate_card(card)
    assert any(e["reason"] == "answered_after_unanswered_marker" for e in errors)
