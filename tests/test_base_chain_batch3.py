"""Batch 3 of the Base read-only RPC pack: 5 real-network cases per route,
verified against known Base mainnet values (USDC, a real Compound cUSDC v3
EIP-1967 proxy, a real Basename, Based Fellas and StarNFTV4 NFT
collections). No mocks.
"""
import pytest

from app.base_chain.registry import RpcComputeError
from app.base_chain.routes.basename import BasenameInput, compute_basename
from app.base_chain.routes.contract_meta import AddressInput, compute_contract, compute_proxy
from app.base_chain.routes.nft import (
    Erc721TokensInput, NftTokenInput, compute_erc721_tokens, compute_nft_metadata, compute_nft_owner,
)
from app.base_chain.routes.raw_rpc import CallInput, StorageInput, compute_call, compute_storage

USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"
CUSDC_V3_PROXY = "0xb125e6687d4313864e53df431d5425969c15eb2f"
CUSDC_V3_IMPL = "0xc1455ae6df6cd808ed677f048e434e22892682a7"
BASED_FELLAS = "0x217Ec1aC929a17481446a76Ff9B95B9A64f298Cf"
BASED_FELLAS_HOLDER = "0x8d233f58c083380a8c5d1a9eb8fca4a403c863fe"
STAR_NFT = "0xdcfeb48770c42a20428f025a69c093155829a11c"
STAR_NFT_HOLDER = "0xf70da97812CB96acDF810712Aa562db8dfA3dbEF"
EIP1967_IMPL_SLOT = "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"


# ------------------------------------------------------------------- base/basename
@pytest.mark.asyncio
async def test_basename_jesse_resolves():
    r = await compute_basename(BasenameInput(name="jesse.base.eth"))
    assert r.address.lower() == "0x2211d1d0020daea8039e46cf1367962070d77da9"


@pytest.mark.asyncio
async def test_basename_base_resolves():
    r = await compute_basename(BasenameInput(name="base.base.eth"))
    assert r.address.lower() == "0x97bcd93504d89d9d236e773e8f5c20feae466e72"


@pytest.mark.asyncio
async def test_basename_unregistered_name_is_422():
    with pytest.raises(RpcComputeError):
        await compute_basename(BasenameInput(name="coinbase.base.eth"))


@pytest.mark.asyncio
async def test_basename_case_insensitive():
    r1 = await compute_basename(BasenameInput(name="Jesse.Base.Eth"))
    r2 = await compute_basename(BasenameInput(name="jesse.base.eth"))
    assert r1.address == r2.address


@pytest.mark.asyncio
async def test_basename_malformed_rejected():
    with pytest.raises(Exception):
        BasenameInput(name="not-a-basename")


# ------------------------------------------------------------------- base/contract
@pytest.mark.asyncio
async def test_contract_usdc_has_owner():
    r = await compute_contract(AddressInput(address=USDC))
    assert r.is_contract is True
    assert r.bytecode_size == 1852
    assert r.owner.lower() == "0x3abd6f64a422225e61e435bae41db12096106df7"
    assert r.is_proxy is False


@pytest.mark.asyncio
async def test_contract_multicall3_no_owner():
    r = await compute_contract(AddressInput(address=MULTICALL3))
    assert r.is_contract is True
    assert r.owner is None


@pytest.mark.asyncio
async def test_contract_proxy_flag_true_for_cusdc():
    r = await compute_contract(AddressInput(address=CUSDC_V3_PROXY))
    assert r.is_proxy is True


@pytest.mark.asyncio
async def test_contract_eoa_not_a_contract():
    r = await compute_contract(AddressInput(address=STAR_NFT_HOLDER))
    assert r.is_contract is False
    assert r.owner is None


@pytest.mark.asyncio
async def test_contract_malformed_address_rejected():
    with pytest.raises(Exception):
        AddressInput(address="bad")


# ---------------------------------------------------------------------- base/proxy
@pytest.mark.asyncio
async def test_proxy_cusdc_eip1967_exact():
    r = await compute_proxy(AddressInput(address=CUSDC_V3_PROXY))
    assert r.is_proxy is True
    assert r.proxy_type == "eip1967"
    assert r.implementation.lower() == CUSDC_V3_IMPL


@pytest.mark.asyncio
async def test_proxy_usdc_is_not_a_proxy():
    r = await compute_proxy(AddressInput(address=USDC))
    assert r.is_proxy is False
    assert r.proxy_type == "none"
    assert r.implementation is None


@pytest.mark.asyncio
async def test_proxy_multicall3_is_not_a_proxy():
    r = await compute_proxy(AddressInput(address=MULTICALL3))
    assert r.is_proxy is False


@pytest.mark.asyncio
async def test_proxy_eoa_is_not_a_proxy():
    r = await compute_proxy(AddressInput(address=STAR_NFT_HOLDER))
    assert r.is_proxy is False


@pytest.mark.asyncio
async def test_proxy_malformed_address_rejected():
    with pytest.raises(Exception):
        AddressInput(address="0x1234")


# -------------------------------------------------------------------- base/storage
@pytest.mark.asyncio
async def test_storage_cusdc_implementation_slot_exact():
    r = await compute_storage(StorageInput(address=CUSDC_V3_PROXY, slot=EIP1967_IMPL_SLOT))
    assert r.value.lower().endswith(CUSDC_V3_IMPL[2:].lower())


@pytest.mark.asyncio
async def test_storage_usdc_same_slot_is_zero():
    r = await compute_storage(StorageInput(address=USDC, slot=EIP1967_IMPL_SLOT))
    assert int(r.value, 16) == 0
    assert r.value_decimal == "0"


@pytest.mark.asyncio
async def test_storage_accepts_int_slot():
    r = await compute_storage(StorageInput(address=USDC, slot=0))
    assert r.slot == "0x0"
    assert r.value.startswith("0x")


@pytest.mark.asyncio
async def test_storage_value_decimal_matches_hex():
    r = await compute_storage(StorageInput(address=CUSDC_V3_PROXY, slot=EIP1967_IMPL_SLOT))
    assert int(r.value, 16) == int(r.value_decimal)


@pytest.mark.asyncio
async def test_storage_malformed_address_rejected():
    with pytest.raises(Exception):
        StorageInput(address="bad", slot=0)


# ----------------------------------------------------------------------- base/call
@pytest.mark.asyncio
async def test_call_usdc_decimals():
    r = await compute_call(CallInput(to=USDC, data="0x313ce567"))
    assert int(r.return_data, 16) == 6


@pytest.mark.asyncio
async def test_call_usdc_balance_of():
    data = "0x70a08231000000000000000000000000" + BASED_FELLAS_HOLDER[2:].lower()
    r = await compute_call(CallInput(to=USDC, data=data))
    assert isinstance(int(r.return_data, 16), int)


@pytest.mark.asyncio
async def test_call_reverting_call_is_422():
    # erc1155 balanceOf(address,uint256) selector against USDC - reverts
    bad_data = "0x00fdd58e" + "00" * 32 + "00" * 32
    with pytest.raises(RpcComputeError):
        await compute_call(CallInput(to=USDC, data=bad_data))


@pytest.mark.asyncio
async def test_call_malformed_data_rejected():
    with pytest.raises(Exception):
        CallInput(to=USDC, data="not-hex")


@pytest.mark.asyncio
async def test_call_malformed_to_rejected():
    with pytest.raises(Exception):
        CallInput(to="bad", data="0x")


# ------------------------------------------------------------- base/erc721-tokens
@pytest.mark.asyncio
async def test_erc721_tokens_star_nft_exact():
    r = await compute_erc721_tokens(Erc721TokensInput(token=STAR_NFT, wallet=STAR_NFT_HOLDER))
    assert r.total_owned == 1
    assert r.token_ids == [129]
    assert r.truncated is False


@pytest.mark.asyncio
async def test_erc721_tokens_zero_balance_wallet():
    r = await compute_erc721_tokens(Erc721TokensInput(token=STAR_NFT, wallet=MULTICALL3))
    assert r.total_owned == 0
    assert r.token_ids == []


@pytest.mark.asyncio
async def test_erc721_tokens_non_enumerable_collection_is_422():
    with pytest.raises(RpcComputeError):
        await compute_erc721_tokens(Erc721TokensInput(token=BASED_FELLAS, wallet=BASED_FELLAS_HOLDER))


@pytest.mark.asyncio
async def test_erc721_tokens_non_token_contract_rejected():
    with pytest.raises(RpcComputeError):
        await compute_erc721_tokens(Erc721TokensInput(token=USDC, wallet=STAR_NFT_HOLDER))


@pytest.mark.asyncio
async def test_erc721_tokens_malformed_wallet_rejected():
    with pytest.raises(Exception):
        Erc721TokensInput(token=STAR_NFT, wallet="bad")


# -------------------------------------------------------------- base/nft-metadata
@pytest.mark.asyncio
async def test_nft_metadata_based_fellas_exact():
    r = await compute_nft_metadata(NftTokenInput(token=BASED_FELLAS, token_id=1))
    assert r.token_uri == "ipfs://bafybeigr7b3cbyrhyjnmv6nx7itr7v25ghqqhfzb23owwvtmaj7vh5vlr4/1"


@pytest.mark.asyncio
async def test_nft_metadata_star_nft_exact():
    r = await compute_nft_metadata(NftTokenInput(token=STAR_NFT, token_id=129))
    assert r.token_uri == "https://graphigo.prd.galaxy.eco/metadata/0xdcfeb48770c42a20428f025a69c093155829a11c/129.json"


@pytest.mark.asyncio
async def test_nft_metadata_nonexistent_token_is_422():
    with pytest.raises(RpcComputeError):
        await compute_nft_metadata(NftTokenInput(token=BASED_FELLAS, token_id=999_999_999))


@pytest.mark.asyncio
async def test_nft_metadata_non_nft_contract_rejected():
    with pytest.raises(RpcComputeError):
        await compute_nft_metadata(NftTokenInput(token=USDC, token_id=1))


@pytest.mark.asyncio
async def test_nft_metadata_negative_id_rejected():
    with pytest.raises(Exception):
        NftTokenInput(token=BASED_FELLAS, token_id=-1)


# ------------------------------------------------------------------ base/nft-owner
@pytest.mark.asyncio
async def test_nft_owner_based_fellas_exact():
    r = await compute_nft_owner(NftTokenInput(token=BASED_FELLAS, token_id=1))
    assert r.owner.lower() == BASED_FELLAS_HOLDER.lower()


@pytest.mark.asyncio
async def test_nft_owner_star_nft_exact():
    r = await compute_nft_owner(NftTokenInput(token=STAR_NFT, token_id=129))
    assert r.owner.lower() == STAR_NFT_HOLDER.lower()


@pytest.mark.asyncio
async def test_nft_owner_nonexistent_token_is_422():
    with pytest.raises(RpcComputeError):
        await compute_nft_owner(NftTokenInput(token=BASED_FELLAS, token_id=999_999_999))


@pytest.mark.asyncio
async def test_nft_owner_non_nft_contract_rejected():
    with pytest.raises(RpcComputeError):
        await compute_nft_owner(NftTokenInput(token=USDC, token_id=1))


@pytest.mark.asyncio
async def test_nft_owner_malformed_token_rejected():
    with pytest.raises(Exception):
        NftTokenInput(token="bad", token_id=1)
