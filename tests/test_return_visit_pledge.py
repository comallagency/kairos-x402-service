from app.return_visit_pledge import SAMPLE_PLEDGE, validate_pledge


def test_sample_valid():
    normalized, errors = validate_pledge(SAMPLE_PLEDGE)
    assert errors == []
    assert normalized is not None
    assert normalized["pledgor"] == SAMPLE_PLEDGE["pledgor"]


def test_return_by_before_pledged():
    pledge = {
        **SAMPLE_PLEDGE,
        "pledged_at": "2026-09-19T12:00:00+00:00",
        "return_by": "2026-09-18T12:00:00+00:00",
    }
    _, errors = validate_pledge(pledge)
    assert any(e["reason"] == "return_by_not_after_pledged_at" for e in errors)


def test_unknown_field():
    pledge = {**SAMPLE_PLEDGE, "extra": 1}
    _, errors = validate_pledge(pledge)
    assert any(e["reason"] == "unknown_field" for e in errors)
