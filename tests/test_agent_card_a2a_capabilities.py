"""Wellknown et d'autres indexeurs A2A refusent capabilities en URL string."""

from app.discovery import _agent_card


def test_agent_card_capabilities_object_not_url() -> None:
    card = _agent_card()
    caps = card["capabilities"]
    assert isinstance(caps, dict)
    assert caps.get("streaming") is False
    assert "capabilitiesUrl" in card
    assert card["capabilitiesUrl"].endswith("/capabilities")
    assert card.get("protocolVersion")
