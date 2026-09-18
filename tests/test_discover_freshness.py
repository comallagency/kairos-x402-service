from datetime import date

from app.discover_freshness import combined_relevance, freshness_factor, parse_observed_day


def test_parse_observed_day() -> None:
    assert parse_observed_day("2026-09-01") == date(2026, 9, 1)
    assert parse_observed_day("2026-09-01T12:00:00Z") == date(2026, 9, 1)
    assert parse_observed_day("") is None


def test_freshness_prefers_recent() -> None:
    ref = date(2026, 9, 18)
    recent = freshness_factor(date(2026, 9, 10), ref)
    old = freshness_factor(date(2025, 1, 1), ref)
    assert recent > old


def test_combined_relevance_uses_freshness() -> None:
    ref = date(2026, 9, 18)
    a = combined_relevance(0.8, "2026-09-17", ref)
    b = combined_relevance(0.8, "2020-01-01", ref)
    assert a > b
