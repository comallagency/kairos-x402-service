"""Score de fraîcheur pour le classement discover (snapshot MCP)."""

from __future__ import annotations

from datetime import date, datetime


def parse_observed_day(value: str | None) -> date | None:
    if not value or not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        if "T" in raw:
            return datetime.fromisoformat(raw).date()
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def freshness_factor(observed: date | None, reference: date) -> float:
    """0–1 : plus récent = plus proche de 1. Sans date → neutre (0,5)."""
    if observed is None:
        return 0.5
    days = (reference - observed).days
    if days < 0:
        days = 0
    return 1.0 / (1.0 + days / 60.0)


def combined_relevance(similarity: float, observed_at: str | None, reference: date) -> float:
    """Mélange sémantique (85 %) et fraîcheur (15 %) pour le tri."""
    fresh = freshness_factor(parse_observed_day(observed_at), reference)
    return float(similarity) * (0.85 + 0.15 * fresh)
