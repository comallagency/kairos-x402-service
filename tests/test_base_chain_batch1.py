"""Batch 1 of the Base read-only RPC pack: 5 real-network cases per route,
verified against known Base mainnet values (genesis block, USDC contract,
two real settlement transactions from this session). No mocks - these hit
the real public RPC, same as the deployed routes will.
"""
import pytest

from app.base_chain.registry import RpcComputeError
from app.base_chain.routes.accounts import AddressInput, compute_code, compute_nonce
from app.base_chain.routes.blocks import BlockInput, PendingInput, compute_block, compute_pending
from app.base_chain.routes.chain_meta import (
    BlockNumberInput, ChainIdInput, NetworkInfoInput,
    compute_block_number, compute_chain_id, compute_network_info,
)
from app.base_chain.routes.transactions import HashInput, compute_receipt, compute_tx

USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"[:42]
TX1 = "0xc2490a8a0aedd1196617a0e52111f82d6059986db7f1713ab23221c91c42f5c4"
TX2 = "0xfefa91bc4a84c11f0c752d4b0b6a61d27e1025b864b604b8585edc1d8468c561"


# ------------------------------------------------------------- base/block-number
@pytest.mark.asyncio
async def test_block_number_above_known_floor():
    r = await compute_block_number(BlockNumberInput())
    assert r.block_number > 52000000  # real height at write time was ~52.1M


@pytest.mark.asyncio
async def test_block_number_hex_consistency():
    r = await compute_block_number(BlockNumberInput())
    assert int(r.block_number_hex, 16) == r.block_number


@pytest.mark.asyncio
async def test_block_number_is_int():
    r = await compute_block_number(BlockNumberInput())
    assert isinstance(r.block_number, int)


@pytest.mark.asyncio
async def test_block_number_hex_prefix():
    r = await compute_block_number(BlockNumberInput())
    assert r.block_number_hex.startswith("0x")


@pytest.mark.asyncio
async def test_block_number_monotonic_non_decreasing():
    r1 = await compute_block_number(BlockNumberInput())
    r2 = await compute_block_number(BlockNumberInput())
    assert r2.block_number >= r1.block_number


# ----------------------------------------------------------------- base/chain-id
@pytest.mark.asyncio
async def test_chain_id_is_8453():
    r = await compute_chain_id(ChainIdInput())
    assert r.chain_id == 8453


@pytest.mark.asyncio
async def test_chain_id_hex_is_0x2105():
    r = await compute_chain_id(ChainIdInput())
    assert r.chain_id_hex == "0x2105"


@pytest.mark.asyncio
async def test_chain_id_caip2():
    r = await compute_chain_id(ChainIdInput())
    assert r.caip2 == "eip155:8453"


@pytest.mark.asyncio
async def test_chain_id_consistent_across_calls():
    r1 = await compute_chain_id(ChainIdInput())
    r2 = await compute_chain_id(ChainIdInput())
    assert r1.chain_id == r2.chain_id


@pytest.mark.asyncio
async def test_chain_id_type():
    r = await compute_chain_id(ChainIdInput())
    assert isinstance(r.chain_id, int)


# ------------------------------------------------------------- base/network-info
@pytest.mark.asyncio
async def test_network_info_chain_id():
    r = await compute_network_info(NetworkInfoInput())
    assert r.chain_id == 8453


@pytest.mark.asyncio
async def test_network_info_block_above_floor():
    r = await compute_network_info(NetworkInfoInput())
    assert r.block_number > 52000000


@pytest.mark.asyncio
async def test_network_info_gas_price_positive():
    r = await compute_network_info(NetworkInfoInput())
    assert r.gas_price_wei > 0


@pytest.mark.asyncio
async def test_network_info_gwei_matches_wei():
    r = await compute_network_info(NetworkInfoInput())
    assert r.gas_price_gwei == round(r.gas_price_wei / 1_000_000_000, 6)


@pytest.mark.asyncio
async def test_network_info_matches_separate_calls():
    r = await compute_network_info(NetworkInfoInput())
    r2 = await compute_block_number(BlockNumberInput())
    assert abs(r.block_number - r2.block_number) < 50  # same chain, a few blocks apart at most


# ------------------------------------------------------------------- base/block
@pytest.mark.asyncio
async def test_block_genesis_exact():
    r = await compute_block(BlockInput(number=0))
    assert r.hash == "0xf712aa9241cc24369b143cf6dce85f0902a9731e70d66818a3a5845b296c73dd"
    assert r.timestamp == 1686789347  # int(0x648a5ce3, 16), verified independently
    assert r.miner == "0x4200000000000000000000000000000000000011"
    assert r.transaction_count == 0


@pytest.mark.asyncio
async def test_block_latest_tag():
    r = await compute_block(BlockInput(number="latest"))
    assert r.number > 52000000


@pytest.mark.asyncio
async def test_block_known_historical_number():
    r = await compute_block(BlockInput(number=52118214))
    assert r.number == 52118214
    assert TX1.lower() in [h.lower() for h in r.transaction_hashes]


@pytest.mark.asyncio
async def test_block_negative_number_rejected():
    with pytest.raises(RpcComputeError):
        await compute_block(BlockInput(number=-1))


@pytest.mark.asyncio
async def test_block_far_future_not_found():
    with pytest.raises(RpcComputeError):
        await compute_block(BlockInput(number=999_999_999_999))


# ----------------------------------------------------------------- base/pending
@pytest.mark.asyncio
async def test_pending_number_at_or_above_latest():
    latest = await compute_block_number(BlockNumberInput())
    r = await compute_pending(PendingInput())
    assert r.number >= latest.block_number


@pytest.mark.asyncio
async def test_pending_transaction_count_non_negative():
    r = await compute_pending(PendingInput())
    assert r.transaction_count >= 0


@pytest.mark.asyncio
async def test_pending_sequencer_miner():
    r = await compute_pending(PendingInput())
    assert r.miner.lower() == "0x4200000000000000000000000000000000000011"


@pytest.mark.asyncio
async def test_pending_hash_well_formed():
    r = await compute_pending(PendingInput())
    assert r.hash.startswith("0x") and len(r.hash) == 66


@pytest.mark.asyncio
async def test_pending_repeatable():
    r1 = await compute_pending(PendingInput())
    r2 = await compute_pending(PendingInput())
    assert r2.number >= r1.number


# ------------------------------------------------------------------- base/nonce
@pytest.mark.asyncio
async def test_nonce_usdc_contract_exact():
    r = await compute_nonce(AddressInput(address=USDC))
    assert r.nonce == 1


@pytest.mark.asyncio
async def test_nonce_zero_address():
    r = await compute_nonce(AddressInput(address=ZERO_ADDRESS))
    assert r.nonce == 8  # real measured value on Base mainnet, not assumed


@pytest.mark.asyncio
async def test_nonce_invalid_address_rejected():
    with pytest.raises(Exception):
        AddressInput(address="not-an-address")


@pytest.mark.asyncio
async def test_nonce_short_address_rejected():
    with pytest.raises(Exception):
        AddressInput(address="0x1234")


@pytest.mark.asyncio
async def test_nonce_multicall3_contract():
    r = await compute_nonce(AddressInput(address=MULTICALL3))
    assert r.nonce == 1  # EIP-161: a contract's nonce starts at 1 at deployment, not 0


# -------------------------------------------------------------------- base/code
@pytest.mark.asyncio
async def test_code_usdc_is_contract_exact_size():
    r = await compute_code(AddressInput(address=USDC))
    assert r.is_contract is True
    assert r.bytecode_size == 1852


@pytest.mark.asyncio
async def test_code_zero_address_not_contract():
    r = await compute_code(AddressInput(address=ZERO_ADDRESS))
    assert r.is_contract is False
    assert r.bytecode_size == 0


@pytest.mark.asyncio
async def test_code_multicall3_is_contract():
    r = await compute_code(AddressInput(address=MULTICALL3))
    assert r.is_contract is True
    assert r.bytecode_size > 0


@pytest.mark.asyncio
async def test_code_invalid_address_rejected():
    with pytest.raises(Exception):
        AddressInput(address="0xzzzz")


@pytest.mark.asyncio
async def test_code_eoa_not_contract():
    # a real wallet used this session for payments - known EOA, never a contract
    r = await compute_code(AddressInput(address="0xb3F32bdfe8D07825BC0D7387295aB1D7559BA69d"))
    assert r.is_contract is False


# ---------------------------------------------------------------------- base/tx
@pytest.mark.asyncio
async def test_tx_known_exact_fields():
    r = await compute_tx(HashInput(hash=TX1))
    assert r.block_number == 52118214
    assert r.from_address.lower() == "0x59b7ebc67a3d627fabaf06768c818638452ae704"
    assert r.to_address.lower() == USDC.lower()
    assert r.value_wei == 0
    assert r.gas == 95905


@pytest.mark.asyncio
async def test_tx_second_known_tx():
    r = await compute_tx(HashInput(hash=TX2))
    assert r.to_address.lower() == USDC.lower()
    assert r.from_address.lower() == "0x8f5cb67b49555e614892b7233cfddebfb746e531"


@pytest.mark.asyncio
async def test_tx_unknown_hash_is_422_not_found():
    fake = "0x" + "ab" * 32
    with pytest.raises(RpcComputeError):
        await compute_tx(HashInput(hash=fake))


@pytest.mark.asyncio
async def test_tx_malformed_hash_rejected_by_schema():
    with pytest.raises(Exception):
        HashInput(hash="0x1234")


@pytest.mark.asyncio
async def test_tx_input_data_present():
    r = await compute_tx(HashInput(hash=TX1))
    assert r.input_data.startswith("0xe3ee160e")


# ---------------------------------------------------------------- base/receipt
@pytest.mark.asyncio
async def test_receipt_known_exact_fields():
    r = await compute_receipt(HashInput(hash=TX1))
    assert r.status is True
    assert r.gas_used == 86262
    assert r.block_number == 52118214
    assert len(r.logs) == 2


@pytest.mark.asyncio
async def test_receipt_log_addresses_are_usdc():
    r = await compute_receipt(HashInput(hash=TX1))
    assert all(lg.address.lower() == USDC.lower() for lg in r.logs)


@pytest.mark.asyncio
async def test_receipt_second_tx_status_true():
    r = await compute_receipt(HashInput(hash=TX2))
    assert r.status is True
    assert r.gas_used == 86230


@pytest.mark.asyncio
async def test_receipt_unknown_hash_is_422():
    fake = "0x" + "cd" * 32
    with pytest.raises(RpcComputeError):
        await compute_receipt(HashInput(hash=fake))


@pytest.mark.asyncio
async def test_receipt_malformed_hash_rejected_by_schema():
    with pytest.raises(Exception):
        HashInput(hash="not-a-hash")
