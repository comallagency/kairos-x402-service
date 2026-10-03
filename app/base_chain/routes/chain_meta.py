"""base/block-number, base/chain-id, base/network-info - no-argument reads
of Base mainnet's current state. See app/base_chain/registry.py."""

from __future__ import annotations

from pydantic import BaseModel

from app.base_chain.registry import BaseRpcSpec, register
from app.base_chain.rpc_client import call, gather


# ---------------------------------------------------------- base/block-number
class BlockNumberInput(BaseModel):
    pass


class BlockNumberOutput(BaseModel):
    block_number: int
    block_number_hex: str


async def compute_block_number(inp: BlockNumberInput) -> BlockNumberOutput:
    hex_result = await call("eth_blockNumber", [])
    return BlockNumberOutput(block_number=int(hex_result, 16), block_number_hex=hex_result)


register(BaseRpcSpec(
    slug="base/block-number", price="$0.001", service_name="base-block-number",
    description="Latest Base mainnet block height via eth_blockNumber - the current chain tip.",
    tags=["base rpc", "block number", "base mainnet", "chain tip", "eth_blockNumber"],
    input_model=BlockNumberInput, output_model=BlockNumberOutput, compute=compute_block_number,
    sample_input={},
    sample_output={"block_number": 51968219, "block_number_hex": "0x31b5cdb"},
))


# -------------------------------------------------------------- base/chain-id
class ChainIdInput(BaseModel):
    pass


class ChainIdOutput(BaseModel):
    chain_id: int
    chain_id_hex: str
    caip2: str


async def compute_chain_id(inp: ChainIdInput) -> ChainIdOutput:
    hex_result = await call("eth_chainId", [])
    chain_id = int(hex_result, 16)
    return ChainIdOutput(chain_id=chain_id, chain_id_hex=hex_result, caip2=f"eip155:{chain_id}")


register(BaseRpcSpec(
    slug="base/chain-id", price="$0.001", service_name="base-chain-id",
    description="Base mainnet chain ID (EIP-155) via eth_chainId - confirm which network you're actually talking to.",
    tags=["base rpc", "chain id", "eip155", "base mainnet", "network identification"],
    input_model=ChainIdInput, output_model=ChainIdOutput, compute=compute_chain_id,
    sample_input={},
    sample_output={"chain_id": 8453, "chain_id_hex": "0x2105", "caip2": "eip155:8453"},
))


# --------------------------------------------------------- base/network-info
class NetworkInfoInput(BaseModel):
    pass


class NetworkInfoOutput(BaseModel):
    chain_id: int
    block_number: int
    gas_price_wei: int
    gas_price_gwei: float


async def compute_network_info(inp: NetworkInfoInput) -> NetworkInfoOutput:
    # eth_blockNumber/eth_chainId/eth_gasPrice are plain JSON-RPC methods,
    # not contract calls - Multicall3 can't batch these (it only batches
    # eth_call), so this groups them as concurrent RPC round trips instead,
    # under one shared timeout (rpc_client.gather), not three.
    chain_id_hex, block_hex, gas_price_hex = await gather(
        call("eth_chainId", []),
        call("eth_blockNumber", []),
        call("eth_gasPrice", []),
    )
    gas_price_wei = int(gas_price_hex, 16)
    return NetworkInfoOutput(
        chain_id=int(chain_id_hex, 16),
        block_number=int(block_hex, 16),
        gas_price_wei=gas_price_wei,
        gas_price_gwei=round(gas_price_wei / 1_000_000_000, 6),
    )


register(BaseRpcSpec(
    slug="base/network-info", price="$0.001", service_name="base-network-info",
    description="Base mainnet network info in one call: chain ID, block height, and current gas price (wei and gwei).",
    tags=["base rpc", "network info", "gas price", "base mainnet", "chain status"],
    input_model=NetworkInfoInput, output_model=NetworkInfoOutput, compute=compute_network_info,
    sample_input={},
    sample_output={"chain_id": 8453, "block_number": 51968219, "gas_price_wei": 6000000, "gas_price_gwei": 0.006},
))
