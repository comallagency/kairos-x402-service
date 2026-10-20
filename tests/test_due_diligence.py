"""Tests for POST /due-diligence - the orchestration logic (skip/error
degradation, overall_verdict rollup), not the 3 underlying routes'
business logic, which already have their own test coverage (or lack
thereof, same as the rest of this repo) independently.

Real on-chain tests tolerate the same pre-existing Base public-RPC latency
regression documented in tests/test_token_card.py (eth_getCode ~3.2-3.4s,
over token_risk.py's own 2.5s enrichment budget) by skipping, not failing,
on "bytecode_unavailable" - this file's signal stays about
app/handlers/due_diligence.py's own logic.
"""

from unittest.mock import AsyncMock, patch

import pytest

from app.handlers.due_diligence import _lookup, _overall_verdict
from app.upstream.evm_rpc import EvmRpcError

USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"


async def _lookup_tolerant(body: dict) -> dict:
    try:
        return await _lookup(body)
    except EvmRpcError as exc:
        if str(exc) == "bytecode_unavailable":
            pytest.skip("base RPC's eth_getCode is currently too slow for token_risk.py's own budget (pre-existing, see test_token_card.py)")
        raise


@pytest.mark.asyncio
async def test_rejects_when_nothing_given():
    with pytest.raises(EvmRpcError) as exc_info:
        await _lookup({})
    assert str(exc_info.value) == "invalid_address"


@pytest.mark.asyncio
async def test_url_only_skips_the_two_address_legs():
    with patch(
        "app.handlers.due_diligence._agent_health_lookup",
        new=AsyncMock(return_value={"verdict": "operational", "score": 100}),
    ):
        result = await _lookup({"url": "https://x402.agentindex.world/search"})
    assert result["address"] is None
    assert result["token"] is None
    assert result["agent_url"] == "https://x402.agentindex.world/search"
    assert result["token_risk"] == {"status": "skipped", "reason": "no_address_or_token_provided"}
    assert result["wallet_intelligence"] == {"status": "skipped", "reason": "no_address_provided"}
    assert result["agent_health"]["verdict"] == "operational"


@pytest.mark.asyncio
async def test_address_only_skips_agent_health():
    result = await _lookup_tolerant({"address": USDC})
    assert result["address"].lower() == USDC.lower()
    assert result["token"].lower() == USDC.lower()  # defaults to address when no separate token given
    assert result["agent_health"] == {"status": "skipped", "reason": "no_url_provided"}
    assert result["token_risk"]["address"].lower() == USDC.lower()
    assert result["wallet_intelligence"]["address"].lower() == USDC.lower()


@pytest.mark.asyncio
async def test_separate_token_overrides_address_for_token_risk():
    other_token = "0x4ed4E862860beD51a9570b96d89aF5E1B0Efefed"  # DEGEN, real Base token
    result = await _lookup_tolerant({"address": USDC, "token": other_token})
    assert result["address"].lower() == USDC.lower()
    assert result["token"].lower() == other_token.lower()
    assert result["wallet_intelligence"]["address"].lower() == USDC.lower()
    assert result["token_risk"]["address"].lower() == other_token.lower()


@pytest.mark.asyncio
async def test_invalid_address_propagates_as_leg_error_not_a_crash():
    with patch(
        "app.handlers.due_diligence._agent_health_lookup",
        new=AsyncMock(return_value={"verdict": "operational"}),
    ):
        result = await _lookup({"address": "not-an-address", "url": "https://example.com"})
    assert result["token_risk"] == {"status": "error", "reason": "invalid_address"}
    assert result["wallet_intelligence"] == {"status": "error", "reason": "invalid_address"}
    assert result["overall_verdict"] == "caution"


def test_overall_verdict_insufficient_data_when_everything_skipped():
    skipped = {"status": "skipped"}
    assert _overall_verdict(skipped, skipped, skipped) == "insufficient_data"


def test_overall_verdict_avoid_beats_everything():
    token_risk = {"verdict": {"verdict": "avoid"}}
    wallet_intelligence = {"x402_readiness": {"can_pay": True}}
    agent_health = {"verdict": "operational"}
    assert _overall_verdict(token_risk, wallet_intelligence, agent_health) == "avoid"


def test_overall_verdict_unreachable_agent_is_avoid():
    token_risk = {"status": "skipped"}
    wallet_intelligence = {"status": "skipped"}
    agent_health = {"verdict": "unreachable"}
    assert _overall_verdict(token_risk, wallet_intelligence, agent_health) == "avoid"


def test_overall_verdict_error_caps_at_caution():
    token_risk = {"status": "error", "reason": "rpc_unavailable"}
    wallet_intelligence = {"status": "skipped"}
    agent_health = {"status": "skipped"}
    assert _overall_verdict(token_risk, wallet_intelligence, agent_health) == "caution"


def test_overall_verdict_acceptable_when_every_leg_ran_clean():
    token_risk = {"verdict": {"verdict": "acceptable"}}
    wallet_intelligence = {"x402_readiness": {"can_pay": True}}
    agent_health = {"verdict": "operational"}
    assert _overall_verdict(token_risk, wallet_intelligence, agent_health) == "acceptable"


def test_overall_verdict_cannot_pay_is_caution():
    token_risk = {"status": "skipped"}
    wallet_intelligence = {"x402_readiness": {"can_pay": False}}
    agent_health = {"status": "skipped"}
    assert _overall_verdict(token_risk, wallet_intelligence, agent_health) == "caution"
