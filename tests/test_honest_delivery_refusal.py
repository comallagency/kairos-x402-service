from app.honest_delivery_refusal import SAMPLE_REFUSAL, validate_refusal


def test_sample_valid():
    normalized, errors = validate_refusal(SAMPLE_REFUSAL)
    assert errors == []
    assert normalized is not None
    assert normalized["code"] == "upstream_unavailable"


def test_missing_summary():
    bad = {**SAMPLE_REFUSAL, "summary": ""}
    _, errors = validate_refusal(bad)
    assert any(e["path"] == "summary" for e in errors)


def test_invalid_code():
    bad = {**SAMPLE_REFUSAL, "code": "made_up"}
    _, errors = validate_refusal(bad)
    assert any(e["path"] == "code" for e in errors)


def test_unknown_field():
    bad = {**SAMPLE_REFUSAL, "extra": 1}
    _, errors = validate_refusal(bad)
    assert any(e["reason"] == "unknown_field" for e in errors)
