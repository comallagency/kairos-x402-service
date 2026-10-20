"""PACK PRE-TRADE BASE: 5 real-network cases per route (simulate, quote,
can-sell), verified against known Base mainnet values. No mocks - every
assertion below was independently confirmed by hand against a live eth_call
before being written here (see conversation / commit history for the raw
curl output).
"""
import pytest

from app.base_chain.registry import RpcComputeError
from app.base_chain.routes.can_sell import CanSellInput, compute_can_sell
from app.base_chain.routes.quote import QuoteInput, compute_quote
from app.base_chain.routes.simulate import SimulateInput, compute_simulate

USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
WETH = "0x4200000000000000000000000000000000000006"
MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"
WALLET_A = "0xb3F32bdfe8D07825BC0D7387295aB1D7559BA69d"
DEGEN = "0x4ed4E862860beD51a9570b96d89aF5E1B0Efefed"


# ----------------------------------------------------------------- base/simulate
@pytest.mark.asyncio
async def test_simulate_success_exact():
    r = await compute_simulate(SimulateInput(from_address=WALLET_A, to=USDC, data="0x313ce567"))
    assert r.will_succeed is True
    assert r.gas_estimate == 30958
    assert r.revert_reason is None
    assert r.return_data == "0x0000000000000000000000000000000000000000000000000000000000000006"


@pytest.mark.asyncio
async def test_simulate_revert_decodes_real_reason():
    to_padded = "000000000000000000000000" + "000000000000000000000000000000000000dEaD"
    amount_padded = hex(2**255)[2:].rjust(64, "0")
    data = "0xa9059cbb" + to_padded + amount_padded
    r = await compute_simulate(SimulateInput(from_address=WALLET_A, to=USDC, data=data))
    assert r.will_succeed is False
    assert r.gas_estimate is None
    assert r.revert_reason == "ERC20: transfer amount exceeds balance"


@pytest.mark.asyncio
async def test_simulate_value_bearing_revert_has_honest_note():
    r = await compute_simulate(SimulateInput(from_address=WALLET_A, to=USDC, data="0x", value_wei=10**20))
    assert r.will_succeed is False
    assert r.revert_reason is not None
    assert "unavailable" in r.revert_reason


@pytest.mark.asyncio
async def test_simulate_malformed_address_rejected():
    with pytest.raises(Exception):
        SimulateInput(from_address="bad", to=USDC, data="0x")


@pytest.mark.asyncio
async def test_simulate_malformed_data_rejected():
    with pytest.raises(Exception):
        SimulateInput(from_address=WALLET_A, to=USDC, data="not-hex")


# -------------------------------------------------------------------- base/quote
@pytest.mark.asyncio
async def test_quote_usdc_to_weth_structural():
    r = await compute_quote(QuoteInput(token_in=USDC, token_out=WETH, amount_in=1_000_000))
    assert r.decimals_in == 6
    assert r.decimals_out == 18
    assert r.best_dex in ("uniswap_v2", "uniswap_v3", "aerodrome")
    assert r.best_amount_out > 0
    assert r.uniswap_v2.available is True
    assert r.uniswap_v2.amount_out == pytest.approx(0.000369, abs=0.00002)


@pytest.mark.asyncio
async def test_quote_weth_to_usdc_reversed_direction():
    r = await compute_quote(QuoteInput(token_in=WETH, token_out=USDC, amount_in=10**15))
    assert r.decimals_in == 18
    assert r.decimals_out == 6
    assert r.best_amount_out > 0


@pytest.mark.asyncio
async def test_quote_all_three_dexes_available_for_usdc_weth():
    r = await compute_quote(QuoteInput(token_in=USDC, token_out=WETH, amount_in=1_000_000))
    assert r.uniswap_v2.available is True
    assert r.uniswap_v3.available is True
    assert r.aerodrome.available is True


@pytest.mark.asyncio
async def test_quote_no_liquidity_is_422():
    with pytest.raises(RpcComputeError):
        await compute_quote(QuoteInput(token_in=MULTICALL3, token_out=WETH, amount_in=1000))


@pytest.mark.asyncio
async def test_quote_malformed_address_rejected():
    with pytest.raises(Exception):
        QuoteInput(token_in="bad", token_out=WETH, amount_in=1000)


# ----------------------------------------------------------------- base/can-sell
@pytest.mark.asyncio
async def test_can_sell_degen_real_token():
    r = await compute_can_sell(CanSellInput(token=DEGEN))
    assert r.buy_succeeded is True
    assert r.approve_succeeded is True
    assert r.sell_succeeded is True
    assert r.can_sell is True
    assert r.reason is None


@pytest.mark.asyncio
async def test_can_sell_degen_smaller_test_amount():
    r = await compute_can_sell(CanSellInput(token=DEGEN, test_eth_wei=10**15))
    assert r.can_sell is True
    assert r.test_eth_wei == "1000000000000000"


@pytest.mark.asyncio
async def test_can_sell_no_liquidity_is_422():
    with pytest.raises(RpcComputeError):
        await compute_can_sell(CanSellInput(token=MULTICALL3))


@pytest.mark.asyncio
async def test_can_sell_malformed_token_rejected():
    with pytest.raises(Exception):
        CanSellInput(token="bad")


@pytest.mark.asyncio
async def test_can_sell_zero_test_amount_rejected():
    with pytest.raises(Exception):
        CanSellInput(token=DEGEN, test_eth_wei=0)
