import asyncio

from app.handlers.fact_check import (
    FACT_CHECK_SAMPLE_RESPONSE,
    SAMPLE_CLAIM,
    fact_check_sample,
)


def test_fact_check_sample_is_static() -> None:
    body = asyncio.run(fact_check_sample())
    assert body["claim"] == SAMPLE_CLAIM
    assert body == FACT_CHECK_SAMPLE_RESPONSE
    assert body["x402_receipt"]["price_paid_usdc"] == 0.0
