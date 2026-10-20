"""base/erc721-tokens, base/nft-metadata, base/nft-owner - ERC721 reads.
See app/base_chain/registry.py.

base/erc721-tokens needs ERC721Enumerable (tokenOfOwnerByIndex) to list a
wallet's held tokens - checked directly against three real, popular Base
NFT collections (2026-10-03) before writing this: Based Fellas, Base
Builder NFT, and the Basenames registry itself all revert on
tokenOfOwnerByIndex (none implement Enumerable - common on Base, where
gas-optimized ERC721A-style contracts are the norm and skip it). A fourth,
StarNFTV4 (0xdcfeb48770c42a20428f025a69c093155829a11c), does implement it
and is this route's real test fixture. There is no RPC-only way to list
"all tokens a wallet currently holds" for a non-Enumerable collection
without an off-chain indexer (scanning Transfer logs only covers the
queried block range, not full history, so it cannot prove CURRENT
holdings - we'd rather return a clear, honest 422 than a silently
incomplete answer). So: not_enumerable is a real, expected, correct
outcome for most Base collections, not a bug.

base/nft-metadata deliberately returns tokenURI() as-is, never fetches it
server-side: an arbitrary tokenURI can point anywhere (IPFS, an HTTP
server with no latency guarantee, something slow or hostile) - fetching it
would break the pack's own 500ms/3s latency contract and is exactly the
kind of "API tierce" dependency the pack is built to avoid. The on-chain
URI pointer *is* the standard's definition of NFT metadata (ERC721 defines
it as an off-chain JSON document by design); resolving that document is
the caller's job.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from app.base_chain.registry import BaseRpcSpec, RpcComputeError, register
from app.base_chain.rpc_client import (
    decode_address,
    decode_uint256,
    encode_address_arg,
    encode_uint256_arg,
    is_valid_address,
    multicall,
    selector,
)

_BALANCE_OF_SEL = selector("balanceOf(address)")
_TOKEN_OF_OWNER_BY_INDEX_SEL = selector("tokenOfOwnerByIndex(address,uint256)")
_OWNER_OF_SEL = selector("ownerOf(uint256)")
_TOKEN_URI_SEL = selector("tokenURI(uint256)")
MAX_TOKENS_LISTED = 20


def _check_address(v: str) -> str:
    if not is_valid_address(v):
        raise ValueError("must be a 0x-prefixed 20-byte hex string")
    return v


def _decode_string(data: bytes) -> str:
    if len(data) < 64:
        return ""
    length = decode_uint256(data[32:64])
    return data[64 : 64 + length].decode("utf8", errors="replace")


# ------------------------------------------------------------- base/erc721-tokens
class Erc721TokensInput(BaseModel):
    token: str
    wallet: str

    @field_validator("token", "wallet")
    @classmethod
    def _validate(cls, v):
        return _check_address(v)


class Erc721TokensOutput(BaseModel):
    token: str
    wallet: str
    total_owned: int
    token_ids: list[int]
    truncated: bool


async def compute_erc721_tokens(inp: Erc721TokensInput) -> Erc721TokensOutput:
    bal_results = await multicall([(inp.token, True, _BALANCE_OF_SEL + encode_address_arg(inp.wallet))])
    bal_ok, bal_data = bal_results[0]
    if not bal_ok:
        raise RpcComputeError("not_an_erc721_token", f"balanceOf(address) reverted on {inp.token}")
    total_owned = decode_uint256(bal_data)
    if total_owned == 0:
        return Erc721TokensOutput(token=inp.token, wallet=inp.wallet, total_owned=0, token_ids=[], truncated=False)

    fetch_count = min(total_owned, MAX_TOKENS_LISTED)
    calls = [
        (inp.token, True, _TOKEN_OF_OWNER_BY_INDEX_SEL + encode_address_arg(inp.wallet) + encode_uint256_arg(i))
        for i in range(fetch_count)
    ]
    results = await multicall(calls)
    first_ok, _ = results[0]
    if not first_ok:
        raise RpcComputeError(
            "not_enumerable",
            f"{inp.token} does not implement ERC721Enumerable (tokenOfOwnerByIndex) - cannot list held tokens via RPC alone",
        )
    token_ids = [decode_uint256(data) for ok, data in results if ok]
    return Erc721TokensOutput(
        token=inp.token, wallet=inp.wallet, total_owned=total_owned,
        token_ids=token_ids, truncated=total_owned > MAX_TOKENS_LISTED,
    )


register(BaseRpcSpec(
    slug="base/erc721-tokens", price="$0.008", service_name="base-nft-holdings",
    description="NFT holdings for a wallet in one Base ERC721 collection, owned token IDs via ERC721Enumerable (tokenOfOwnerByIndex) - up to 20 tokens, requires the collection to implement Enumerable.",
    tags=["erc721 tokens base", "nft holdings", "base rpc", "tokenOfOwnerByIndex", "base mainnet"],
    input_model=Erc721TokensInput, output_model=Erc721TokensOutput, compute=compute_erc721_tokens,
    sample_input={"token": "0xdcfeb48770c42a20428f025a69c093155829a11c", "wallet": "0xf70da97812CB96acDF810712Aa562db8dfA3dbEF"},
    sample_output={
        "token": "0xdcfeb48770c42a20428f025a69c093155829a11c", "wallet": "0xf70da97812CB96acDF810712Aa562db8dfA3dbEF",
        "total_owned": 1, "token_ids": [129], "truncated": False,
    },
))


# -------------------------------------------------------------- base/nft-metadata
class NftTokenInput(BaseModel):
    token: str
    token_id: int = Field(ge=0)

    @field_validator("token")
    @classmethod
    def _validate(cls, v):
        return _check_address(v)


class NftMetadataOutput(BaseModel):
    token: str
    token_id: int
    token_uri: str


async def compute_nft_metadata(inp: NftTokenInput) -> NftMetadataOutput:
    results = await multicall([(inp.token, True, _TOKEN_URI_SEL + encode_uint256_arg(inp.token_id))])
    ok, data = results[0]
    if not ok:
        raise RpcComputeError("token_not_found", f"tokenURI(uint256) reverted - token {inp.token_id} may not exist on {inp.token}")
    return NftMetadataOutput(token=inp.token, token_id=inp.token_id, token_uri=_decode_string(data))


register(BaseRpcSpec(
    slug="base/nft-metadata", price="$0.008", service_name="base-nft-metadata",
    description="NFT metadata URI for an ERC721 token on Base mainnet via tokenURI - the on-chain pointer (IPFS, HTTP, or data URI), not a fetch of the document itself.",
    tags=["nft metadata base", "tokenURI", "base rpc", "erc721 metadata", "base mainnet"],
    input_model=NftTokenInput, output_model=NftMetadataOutput, compute=compute_nft_metadata,
    sample_input={"token": "0x217Ec1aC929a17481446a76Ff9B95B9A64f298Cf", "token_id": 1},
    sample_output={
        "token": "0x217Ec1aC929a17481446a76Ff9B95B9A64f298Cf", "token_id": 1,
        "token_uri": "ipfs://bafybeigr7b3cbyrhyjnmv6nx7itr7v25ghqqhfzb23owwvtmaj7vh5vlr4/1",
    },
))


# ----------------------------------------------------------------- base/nft-owner
class NftOwnerOutput(BaseModel):
    token: str
    token_id: int
    owner: str


async def compute_nft_owner(inp: NftTokenInput) -> NftOwnerOutput:
    results = await multicall([(inp.token, True, _OWNER_OF_SEL + encode_uint256_arg(inp.token_id))])
    ok, data = results[0]
    if not ok:
        raise RpcComputeError("token_not_found", f"ownerOf(uint256) reverted - token {inp.token_id} may not exist on {inp.token}")
    owner = decode_address(data)
    if owner is None:
        raise RpcComputeError("token_not_found", f"ownerOf(uint256) returned the zero address for token {inp.token_id} on {inp.token}")
    return NftOwnerOutput(token=inp.token, token_id=inp.token_id, owner=owner)


register(BaseRpcSpec(
    slug="base/nft-owner", price="$0.003", service_name="base-nft-owner",
    description="Who owns this NFT? ERC721 owner lookup on Base mainnet via ownerOf(tokenId).",
    tags=["nft owner base", "ownerOf", "base rpc", "erc721 owner", "base mainnet"],
    input_model=NftTokenInput, output_model=NftOwnerOutput, compute=compute_nft_owner,
    sample_input={"token": "0xdcfeb48770c42a20428f025a69c093155829a11c", "token_id": 129},
    sample_output={
        "token": "0xdcfeb48770c42a20428f025a69c093155829a11c", "token_id": 129,
        "owner": "0xf70da97812cb96acdf810712aa562db8dfa3dbef",
    },
))
