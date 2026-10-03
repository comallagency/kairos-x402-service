"""Batch 2 of the Base read-only RPC pack: 5 real-network cases per route,
verified against known Base mainnet values (USDC, WETH predeploy, Multicall3,
a BasePaint ERC1155 holder found via a real TransferSingle log, and the exact
Transfer log from batch 1's settlement tx). No mocks.
"""
import pytest

from app.base_chain.registry import RpcComputeError
from app.base_chain.routes.gas import EstimateGasInput, compute_estimate_gas
from app.base_chain.routes.logs import (
    Erc20TransfersInput, EventsInput, compute_erc20_transfers, compute_events,
)
from app.base_chain.routes.tokens import (
    AllowanceInput, Erc1155BalanceInput, LiveBalanceInput, TokenInput, TokenWalletInput,
    compute_allowance, compute_erc1155_balance, compute_erc20_balance, compute_live_balance,
    compute_total_supply,
)

USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
WETH = "0x4200000000000000000000000000000000000006"
MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
WALLET_A = "0xb3F32bdfe8D07825BC0D7387295aB1D7559BA69d"  # 0.027892 USDC, 0 ETH, 0 WETH
BASEPAINT = "0xBa5E05cb26b78eDa3A2f8e3b3814726305DCAC83"
BASEPAINT_HOLDER = "0xdb6882db2A406Bc1541988715842906Dfd4FD590"  # real TransferSingle recipient, id=14, value=1
TX1 = "0xc2490a8a0aedd1196617a0e52111f82d6059986db7f1713ab23221c91c42f5c4"
TX1_BLOCK = 52118214


# ------------------------------------------------------------ base/erc20-balance
@pytest.mark.asyncio
async def test_erc20_balance_usdc_exact():
    r = await compute_erc20_balance(TokenWalletInput(token=USDC, wallet=WALLET_A))
    assert r.decimals == 6
    assert r.balance_raw == "27892"
    assert r.balance == pytest.approx(0.027892)


@pytest.mark.asyncio
async def test_erc20_balance_weth_zero():
    r = await compute_erc20_balance(TokenWalletInput(token=WETH, wallet=WALLET_A))
    assert r.decimals == 18
    assert r.balance_raw == "0"


@pytest.mark.asyncio
async def test_erc20_balance_non_token_rejected():
    with pytest.raises(RpcComputeError):
        await compute_erc20_balance(TokenWalletInput(token=MULTICALL3, wallet=WALLET_A))


@pytest.mark.asyncio
async def test_erc20_balance_malformed_token_rejected():
    with pytest.raises(Exception):
        TokenWalletInput(token="not-an-address", wallet=WALLET_A)


@pytest.mark.asyncio
async def test_erc20_balance_malformed_wallet_rejected():
    with pytest.raises(Exception):
        TokenWalletInput(token=USDC, wallet="0x1234")


# ------------------------------------------------------------- base/live-balance
@pytest.mark.asyncio
async def test_live_balance_native_and_tokens():
    r = await compute_live_balance(LiveBalanceInput(wallet=WALLET_A, tokens=[USDC, WETH]))
    assert r.native_balance_wei == "0"
    assert len(r.tokens) == 2
    assert r.tokens[0].token == USDC and r.tokens[0].balance_raw == "27892"
    assert r.tokens[1].token == WETH and r.tokens[1].balance_raw == "0"


@pytest.mark.asyncio
async def test_live_balance_no_tokens():
    r = await compute_live_balance(LiveBalanceInput(wallet=WALLET_A, tokens=[]))
    assert r.tokens == []
    assert r.native_balance_wei == "0"


@pytest.mark.asyncio
async def test_live_balance_too_many_tokens_rejected():
    with pytest.raises(Exception):
        LiveBalanceInput(wallet=WALLET_A, tokens=[USDC] * 21)


@pytest.mark.asyncio
async def test_live_balance_bad_token_in_list_rejected():
    with pytest.raises(RpcComputeError):
        await compute_live_balance(LiveBalanceInput(wallet=WALLET_A, tokens=[MULTICALL3]))


@pytest.mark.asyncio
async def test_live_balance_malformed_wallet_rejected():
    with pytest.raises(Exception):
        LiveBalanceInput(wallet="bad", tokens=[])


# ------------------------------------------------------------- base/total-supply
@pytest.mark.asyncio
async def test_total_supply_usdc_floor_and_decimals():
    r = await compute_total_supply(TokenInput(token=USDC))
    assert r.decimals == 6
    assert r.total_supply > 1_000_000_000  # USDC supply on Base is billions, fluctuates - floor check


@pytest.mark.asyncio
async def test_total_supply_weth_positive():
    r = await compute_total_supply(TokenInput(token=WETH))
    assert r.decimals == 18
    assert r.total_supply >= 0


@pytest.mark.asyncio
async def test_total_supply_non_token_rejected():
    with pytest.raises(RpcComputeError):
        await compute_total_supply(TokenInput(token=MULTICALL3))


@pytest.mark.asyncio
async def test_total_supply_raw_matches_normalized():
    r = await compute_total_supply(TokenInput(token=USDC))
    assert int(r.total_supply_raw) / 10 ** r.decimals == pytest.approx(r.total_supply)


@pytest.mark.asyncio
async def test_total_supply_malformed_address_rejected():
    with pytest.raises(Exception):
        TokenInput(token="0xnope")


# ---------------------------------------------------------------- base/allowance
@pytest.mark.asyncio
async def test_allowance_zero_address_pair_is_zero():
    r = await compute_allowance(AllowanceInput(token=USDC, owner=ZERO_ADDRESS, spender=ZERO_ADDRESS))
    assert r.allowance_raw == "0"
    assert r.decimals == 6


@pytest.mark.asyncio
async def test_allowance_wallet_never_approved_multicall3():
    r = await compute_allowance(AllowanceInput(token=USDC, owner=WALLET_A, spender=MULTICALL3))
    assert r.allowance_raw == "0"


@pytest.mark.asyncio
async def test_allowance_known_large_approval_pair():
    # real Approval event found on-chain (2026-10-03): owner approved spender
    # close to max uint256 - verified currently > 10**70 (still near-max after
    # partial spend; asserting a huge floor rather than the exact decreasing
    # value, which changes every time the spender draws on it).
    r = await compute_allowance(AllowanceInput(
        token=USDC, owner="0xd54302a758f4C4bfD65a16BC4e1AB97314A37a46", spender="0x03A520b32C04BF3bEEf7BEb72E919cf822Ed34f1",
    ))
    assert int(r.allowance_raw) > 10 ** 70


@pytest.mark.asyncio
async def test_allowance_non_token_rejected():
    with pytest.raises(RpcComputeError):
        await compute_allowance(AllowanceInput(token=MULTICALL3, owner=WALLET_A, spender=ZERO_ADDRESS))


@pytest.mark.asyncio
async def test_allowance_malformed_owner_rejected():
    with pytest.raises(Exception):
        AllowanceInput(token=USDC, owner="bad", spender=ZERO_ADDRESS)


# ----------------------------------------------------------- base/erc1155-balance
@pytest.mark.asyncio
async def test_erc1155_balance_known_holder_exact():
    r = await compute_erc1155_balance(Erc1155BalanceInput(token=BASEPAINT, wallet=BASEPAINT_HOLDER, token_id=14))
    assert r.balance_raw == "1"
    assert r.balance == 1


@pytest.mark.asyncio
async def test_erc1155_balance_wallet_with_no_holdings_is_zero():
    # OpenZeppelin's ERC1155 balanceOf() reverts for the zero address itself
    # ("address zero is not a valid owner") - verified directly on BasePaint
    # (2026-10-03) - so the zero-balance case uses a real wallet instead.
    r = await compute_erc1155_balance(Erc1155BalanceInput(token=BASEPAINT, wallet=WALLET_A, token_id=14))
    assert r.balance_raw == "0"


@pytest.mark.asyncio
async def test_erc1155_balance_non_erc1155_contract_rejected():
    with pytest.raises(RpcComputeError):
        await compute_erc1155_balance(Erc1155BalanceInput(token=USDC, wallet=WALLET_A, token_id=0))


@pytest.mark.asyncio
async def test_erc1155_balance_negative_id_rejected():
    with pytest.raises(Exception):
        Erc1155BalanceInput(token=BASEPAINT, wallet=BASEPAINT_HOLDER, token_id=-1)


@pytest.mark.asyncio
async def test_erc1155_balance_malformed_address_rejected():
    with pytest.raises(Exception):
        Erc1155BalanceInput(token="bad", wallet=BASEPAINT_HOLDER, token_id=14)


# ------------------------------------------------------------ base/erc20-transfers
@pytest.mark.asyncio
async def test_erc20_transfers_finds_known_transfer():
    r = await compute_erc20_transfers(Erc20TransfersInput(token=USDC, from_block=TX1_BLOCK, to_block=TX1_BLOCK))
    assert len(r.transfers) >= 70  # 77 measured 2026-10-03, floor to tolerate reorg/indexing differences
    match = [t for t in r.transfers if t.transaction_hash.lower() == TX1.lower()]
    assert len(match) == 1
    assert match[0].log_index == 234
    assert match[0].from_address.lower() == "0x3cedc3cba49c3809ee46b9bf60da75d6607b45ec"
    assert match[0].to_address.lower() == WALLET_A.lower()
    assert match[0].value_raw == "1000"


@pytest.mark.asyncio
async def test_erc20_transfers_quiet_contract_is_empty():
    r = await compute_erc20_transfers(Erc20TransfersInput(token=MULTICALL3, from_block=TX1_BLOCK, to_block=TX1_BLOCK + 1000))
    assert r.transfers == []


@pytest.mark.asyncio
async def test_erc20_transfers_range_too_wide_rejected():
    with pytest.raises(RpcComputeError):
        await compute_erc20_transfers(Erc20TransfersInput(token=USDC, from_block=0, to_block=3000))


@pytest.mark.asyncio
async def test_erc20_transfers_inverted_range_rejected():
    with pytest.raises(RpcComputeError):
        await compute_erc20_transfers(Erc20TransfersInput(token=USDC, from_block=TX1_BLOCK, to_block=TX1_BLOCK - 1))


@pytest.mark.asyncio
async def test_erc20_transfers_malformed_token_rejected():
    with pytest.raises(Exception):
        Erc20TransfersInput(token="bad", from_block=0, to_block=0)


# -------------------------------------------------------------------- base/events
@pytest.mark.asyncio
async def test_events_finds_known_transfer_log():
    r = await compute_events(EventsInput(
        address=USDC, topics=[logs_transfer_topic()], from_block=TX1_BLOCK, to_block=TX1_BLOCK,
    ))
    match = [lg for lg in r.logs if lg.transaction_hash.lower() == TX1.lower() and lg.log_index == 234]
    assert len(match) == 1
    assert match[0].data == "0x00000000000000000000000000000000000000000000000000000000000003e8"


@pytest.mark.asyncio
async def test_events_global_no_address_filter():
    r = await compute_events(EventsInput(topics=[logs_transfer_topic()], from_block=TX1_BLOCK, to_block=TX1_BLOCK))
    assert len(r.logs) > 77  # at least USDC's own count, likely more contracts emit Transfer in the same block


@pytest.mark.asyncio
async def test_events_range_too_wide_rejected():
    with pytest.raises(RpcComputeError):
        await compute_events(EventsInput(address=USDC, topics=[], from_block=0, to_block=5000))


@pytest.mark.asyncio
async def test_events_malformed_topic_rejected():
    with pytest.raises(Exception):
        EventsInput(address=USDC, topics=["not-a-topic"], from_block=0, to_block=0)


@pytest.mark.asyncio
async def test_events_no_match_is_empty():
    r = await compute_events(EventsInput(address=MULTICALL3, topics=[logs_transfer_topic()], from_block=TX1_BLOCK, to_block=TX1_BLOCK))
    assert r.logs == []


def logs_transfer_topic():
    from app.base_chain.routes.logs import TRANSFER_TOPIC
    return TRANSFER_TOPIC


# ---------------------------------------------------------------- base/estimate-gas
@pytest.mark.asyncio
async def test_estimate_gas_plain_transfer_is_21000():
    r = await compute_estimate_gas(EstimateGasInput(to=BASEPAINT_HOLDER, from_address=WALLET_A, value_wei=0))
    assert r.gas_estimate == 21000


@pytest.mark.asyncio
async def test_estimate_gas_token_transfer_call():
    data = "0xa9059cbb000000000000000000000000db6882db2a406bc1541988715842906dfd4fd5900000000000000000000000000000000000000000000000000000000000000000"
    r = await compute_estimate_gas(EstimateGasInput(to=USDC, from_address=WALLET_A, data=data))
    assert r.gas_estimate == 40162


@pytest.mark.asyncio
async def test_estimate_gas_without_from_address():
    r = await compute_estimate_gas(EstimateGasInput(to=BASEPAINT_HOLDER, value_wei=0))
    assert r.gas_estimate >= 21000


@pytest.mark.asyncio
async def test_estimate_gas_malformed_to_rejected():
    with pytest.raises(Exception):
        EstimateGasInput(to="bad")


@pytest.mark.asyncio
async def test_estimate_gas_malformed_data_rejected():
    with pytest.raises(Exception):
        EstimateGasInput(to=USDC, data="not-hex")
