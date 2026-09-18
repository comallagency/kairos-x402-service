import asyncio

from app.handlers.contact import (
    OPENAPI_MESSAGE_ID_PLACEHOLDER,
    SAMPLE_POLL_ID,
    contact_sample,
    get_contact,
    post_contact_poll,
)


def test_contact_sample_and_poll_match() -> None:
    sample = asyncio.run(contact_sample())
    status = asyncio.run(get_contact(SAMPLE_POLL_ID))

    assert sample["response"]["id"] == status["id"]
    assert status["status"] == "unanswered"


def test_openapi_message_id_placeholder_returns_sample_shape() -> None:
    status = asyncio.run(get_contact(OPENAPI_MESSAGE_ID_PLACEHOLDER))
    assert status["id"] == SAMPLE_POLL_ID


def test_openapi_placeholder_post_matches_get() -> None:
    status = asyncio.run(post_contact_poll(OPENAPI_MESSAGE_ID_PLACEHOLDER))
    assert status["id"] == SAMPLE_POLL_ID
