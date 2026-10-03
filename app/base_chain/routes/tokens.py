"""base/erc20-balance, base/live-balance, base/total-supply, base/allowance,
base/erc1155-balance - ERC20/ERC1155 contract reads via eth_call, batched
through Multicall3 wherever more than one read is needed (balanceOf+decimals
in one round trip, or the native balance plus up to 20 ERC20 balances for
live-balance, in exactly ONE round trip regardless of token count). See
app/base_chain/registry.py.

Every call here goes through multicall(allow_failure=True) even when there
is only one read to make (erc1155-balance), never a raw call("eth_call", ...)
- a wrong/non-matching contract address makes the underlying function call
revert, and Multicall3's Call3.allowFailure=true is what turns that into an
ordinary (success=False, b"") result instead of a JSON-RPC error that would
otherwise need rpc_client.RpcApplicationError handling here too. Verified
directly against mainnet.base.org before writing this: USDC called with the
ERC1155 balanceOf(address,uint256) selector reverts, and multicall() reports
that sub-call's ok=False without the round trip itself failing.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from app.base_chain.registry import BaseRpcSpec, RpcComputeError, register
from app.base_chain.rpc_client import (
    MULTICALL3,
    decode_uint256,
    encode_address_arg,
    encode_uint256_arg,
    is_valid_address,
    multicall,
    selector,
)

_BALANCE_OF_SEL = selector("balanceOf(address)")
_DECIMALS_SEL = selector("decimals()")
_TOTAL_SUPPLY_SEL = selector("totalSupply()")
_ALLOWANCE_SEL = selector("allowance(address,address)")
_BALANCE_OF_1155_SEL = selector("balanceOf(address,uint256)")
_GET_ETH_BALANCE_SEL = selector("getEthBalance(address)")


def _check_address(v: str) -> str:
    if not is_valid_address(v):
        raise ValueError("must be a 0x-prefixed 20-byte hex string")
    return v


# ------------------------------------------------------------ base/erc20-balance
class TokenWalletInput(BaseModel):
    token: str
    wallet: str

    @field_validator("token", "wallet")
    @classmethod
    def _validate(cls, v):
        return _check_address(v)


class Erc20BalanceOutput(BaseModel):
    token: str
    wallet: str
    decimals: int
    balance_raw: str
    balance: float


async def compute_erc20_balance(inp: TokenWalletInput) -> Erc20BalanceOutput:
    results = await multicall([
        (inp.token, True, _BALANCE_OF_SEL + encode_address_arg(inp.wallet)),
        (inp.token, True, _DECIMALS_SEL),
    ])
    (bal_ok, bal_data), (dec_ok, dec_data) = results
    if not bal_ok:
        raise RpcComputeError("not_an_erc20_token", f"balanceOf(address) reverted on {inp.token}")
    balance_raw = decode_uint256(bal_data)
    decimals = decode_uint256(dec_data) if dec_ok else 18
    return Erc20BalanceOutput(
        token=inp.token, wallet=inp.wallet, decimals=decimals,
        balance_raw=str(balance_raw), balance=balance_raw / (10 ** decimals),
    )


register(BaseRpcSpec(
    slug="base/erc20-balance", price="$0.003", service_name="base-erc20-balance",
    description="ERC20 token balance for a wallet on Base mainnet - balanceOf() and decimals() batched into one Multicall3 round trip.",
    tags=["erc20 balance base", "token balance", "base rpc", "balanceOf", "base mainnet"],
    input_model=TokenWalletInput, output_model=Erc20BalanceOutput, compute=compute_erc20_balance,
    sample_input={"token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "wallet": "0xb3F32bdfe8D07825BC0D7387295aB1D7559BA69d"},
    sample_output={
        "token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "wallet": "0xb3F32bdfe8D07825BC0D7387295aB1D7559BA69d",
        "decimals": 6, "balance_raw": "27892", "balance": 0.027892,
    },
))


# ------------------------------------------------------------- base/live-balance
class LiveBalanceInput(BaseModel):
    wallet: str
    tokens: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("wallet")
    @classmethod
    def _validate_wallet(cls, v):
        return _check_address(v)

    @field_validator("tokens")
    @classmethod
    def _validate_tokens(cls, v):
        for token in v:
            _check_address(token)
        return v


class TokenBalance(BaseModel):
    token: str
    decimals: int
    balance_raw: str
    balance: float


class LiveBalanceOutput(BaseModel):
    wallet: str
    native_balance_wei: str
    native_balance_eth: float
    tokens: list[TokenBalance]


async def compute_live_balance(inp: LiveBalanceInput) -> LiveBalanceOutput:
    calls = [(MULTICALL3, True, _GET_ETH_BALANCE_SEL + encode_address_arg(inp.wallet))]
    for token in inp.tokens:
        calls.append((token, True, _BALANCE_OF_SEL + encode_address_arg(inp.wallet)))
        calls.append((token, True, _DECIMALS_SEL))

    results = await multicall(calls)
    native_ok, native_data = results[0]
    native_raw = decode_uint256(native_data) if native_ok else 0

    tokens_out = []
    for i, token in enumerate(inp.tokens):
        bal_ok, bal_data = results[1 + i * 2]
        dec_ok, dec_data = results[2 + i * 2]
        if not bal_ok:
            raise RpcComputeError("not_an_erc20_token", f"balanceOf(address) reverted on {token}")
        balance_raw = decode_uint256(bal_data)
        decimals = decode_uint256(dec_data) if dec_ok else 18
        tokens_out.append(TokenBalance(
            token=token, decimals=decimals, balance_raw=str(balance_raw), balance=balance_raw / (10 ** decimals),
        ))

    return LiveBalanceOutput(
        wallet=inp.wallet, native_balance_wei=str(native_raw),
        native_balance_eth=native_raw / 10 ** 18, tokens=tokens_out,
    )


register(BaseRpcSpec(
    slug="base/live-balance", price="$0.003", service_name="base-live-balance",
    description="Native ETH balance plus up to 20 ERC20 token balances for one wallet on Base mainnet, in a single Multicall3 round trip regardless of token count.",
    tags=["live balance base", "multicall", "native and erc20 balance", "base rpc", "portfolio"],
    input_model=LiveBalanceInput, output_model=LiveBalanceOutput, compute=compute_live_balance,
    sample_input={"wallet": "0xb3F32bdfe8D07825BC0D7387295aB1D7559BA69d", "tokens": ["0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "0x4200000000000000000000000000000000000006"]},
    sample_output={
        "wallet": "0xb3F32bdfe8D07825BC0D7387295aB1D7559BA69d", "native_balance_wei": "0", "native_balance_eth": 0.0,
        "tokens": [
            {"token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "decimals": 6, "balance_raw": "27892", "balance": 0.027892},
            {"token": "0x4200000000000000000000000000000000000006", "decimals": 18, "balance_raw": "0", "balance": 0.0},
        ],
    },
))


# ------------------------------------------------------------- base/total-supply
class TokenInput(BaseModel):
    token: str

    @field_validator("token")
    @classmethod
    def _validate(cls, v):
        return _check_address(v)


class TotalSupplyOutput(BaseModel):
    token: str
    total_supply_raw: str
    decimals: int | None
    total_supply: float


async def compute_total_supply(inp: TokenInput) -> TotalSupplyOutput:
    results = await multicall([
        (inp.token, True, _TOTAL_SUPPLY_SEL),
        (inp.token, True, _DECIMALS_SEL),
    ])
    (supply_ok, supply_data), (dec_ok, dec_data) = results
    if not supply_ok:
        raise RpcComputeError("no_total_supply", f"totalSupply() reverted on {inp.token} - not an ERC20/ERC721 contract")
    raw = decode_uint256(supply_data)
    decimals = decode_uint256(dec_data) if dec_ok else None
    normalized = raw / (10 ** decimals) if decimals is not None else float(raw)
    return TotalSupplyOutput(token=inp.token, total_supply_raw=str(raw), decimals=decimals, total_supply=normalized)


register(BaseRpcSpec(
    slug="base/total-supply", price="$0.003", service_name="base-total-supply",
    description="Total supply for an ERC20 (normalized by decimals()) or ERC721 (raw count, no decimals) contract on Base mainnet.",
    tags=["total supply base", "erc20 base", "erc721 base", "base rpc", "totalSupply"],
    input_model=TokenInput, output_model=TotalSupplyOutput, compute=compute_total_supply,
    sample_input={"token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"},
    sample_output={
        "token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "total_supply_raw": "4375417452035492",
        "decimals": 6, "total_supply": 4375417452.035492,
    },
))


# ----------------------------------------------------------------- base/allowance
class AllowanceInput(BaseModel):
    token: str
    owner: str
    spender: str

    @field_validator("token", "owner", "spender")
    @classmethod
    def _validate(cls, v):
        return _check_address(v)


class AllowanceOutput(BaseModel):
    token: str
    owner: str
    spender: str
    decimals: int
    allowance_raw: str
    allowance: float


async def compute_allowance(inp: AllowanceInput) -> AllowanceOutput:
    calldata = _ALLOWANCE_SEL + encode_address_arg(inp.owner) + encode_address_arg(inp.spender)
    results = await multicall([
        (inp.token, True, calldata),
        (inp.token, True, _DECIMALS_SEL),
    ])
    (allow_ok, allow_data), (dec_ok, dec_data) = results
    if not allow_ok:
        raise RpcComputeError("not_an_erc20_token", f"allowance(address,address) reverted on {inp.token}")
    raw = decode_uint256(allow_data)
    decimals = decode_uint256(dec_data) if dec_ok else 18
    return AllowanceOutput(
        token=inp.token, owner=inp.owner, spender=inp.spender, decimals=decimals,
        allowance_raw=str(raw), allowance=raw / (10 ** decimals),
    )


register(BaseRpcSpec(
    slug="base/allowance", price="$0.003", service_name="base-allowance",
    description="ERC20 spending allowance an owner has granted a spender on Base mainnet, via allowance(owner,spender).",
    tags=["allowance base", "erc20 approval", "base rpc", "spending limit", "base mainnet"],
    input_model=AllowanceInput, output_model=AllowanceOutput, compute=compute_allowance,
    sample_input={
        "token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        "owner": "0xb3F32bdfe8D07825BC0D7387295aB1D7559BA69d",
        "spender": "0xcA11bde05977b3631167028862bE2a173976CA11",
    },
    sample_output={
        "token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "owner": "0xb3F32bdfe8D07825BC0D7387295aB1D7559BA69d",
        "spender": "0xcA11bde05977b3631167028862bE2a173976CA11", "decimals": 6, "allowance_raw": "0", "allowance": 0.0,
    },
))


# ------------------------------------------------------------ base/erc1155-balance
class Erc1155BalanceInput(BaseModel):
    token: str
    wallet: str
    token_id: int = Field(ge=0)

    @field_validator("token", "wallet")
    @classmethod
    def _validate(cls, v):
        return _check_address(v)


class Erc1155BalanceOutput(BaseModel):
    token: str
    wallet: str
    token_id: int
    balance_raw: str
    balance: int


async def compute_erc1155_balance(inp: Erc1155BalanceInput) -> Erc1155BalanceOutput:
    calldata = _BALANCE_OF_1155_SEL + encode_address_arg(inp.wallet) + encode_uint256_arg(inp.token_id)
    results = await multicall([(inp.token, True, calldata)])
    ok, data = results[0]
    if not ok:
        raise RpcComputeError("not_an_erc1155_token", f"balanceOf(address,uint256) reverted on {inp.token}")
    raw = decode_uint256(data)
    return Erc1155BalanceOutput(token=inp.token, wallet=inp.wallet, token_id=inp.token_id, balance_raw=str(raw), balance=raw)


register(BaseRpcSpec(
    slug="base/erc1155-balance", price="$0.003", service_name="base-erc1155-balance",
    description="ERC1155 multi-token balance for a wallet and token id on Base mainnet via balanceOf(address,uint256).",
    tags=["erc1155 balance base", "multi token", "base rpc", "nft balance", "base mainnet"],
    input_model=Erc1155BalanceInput, output_model=Erc1155BalanceOutput, compute=compute_erc1155_balance,
    sample_input={"token": "0xBa5E05cb26b78eDa3A2f8e3b3814726305DCAC83", "wallet": "0xdb6882db2A406Bc1541988715842906Dfd4FD590", "token_id": 14},
    sample_output={
        "token": "0xBa5E05cb26b78eDa3A2f8e3b3814726305DCAC83", "wallet": "0xdb6882db2A406Bc1541988715842906Dfd4FD590",
        "token_id": 14, "balance_raw": "1", "balance": 1,
    },
))
