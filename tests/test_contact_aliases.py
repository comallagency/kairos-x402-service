"""Champs alternatifs sur POST /contact (from, message)."""

from app.handlers.contact import ContactIn


def test_contact_in_accepts_from_and_message() -> None:
    payload = ContactIn.model_validate(
        {"from": "agent-alias/1.0", "message": "Hello from a minimal client."}
    )
    assert payload.sender == "agent-alias/1.0"
    assert payload.body == "Hello from a minimal client."
    assert payload.subject.startswith("Hello")


def test_contact_in_prefers_canonical_fields() -> None:
    payload = ContactIn.model_validate(
        {
            "sender": "canonical",
            "from": "ignored",
            "subject": "s",
            "body": "b",
            "message": "ignored",
        }
    )
    assert payload.sender == "canonical"
    assert payload.body == "b"
