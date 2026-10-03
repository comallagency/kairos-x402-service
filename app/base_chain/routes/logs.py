"""base/erc20-transfers, base/events - eth_getLogs based reads.

Base's public RPC providers cap eth_getLogs, but the cap is per-provider
policy, not a chain-wide rule - measured directly (2026-10-03):
base.drpc.org (tried first, see rpc_client._PROVIDER_ORDER) rejects spans
over 50 blocks, mainnet.base.org allows 2,000 for the identical query, and a
sufficiently busy contract/topic (USDC with no address filter) can also hit
a response-size cap within a single block. None of this is pre-checkable
client-side beyond a generous upper bound (MAX_BLOCK_RANGE, matching the
most permissive provider seen) - the real limit for a given request is
discovered by rpc_client.call()'s normal provider fallback, which is
exactly what "plusieurs fournisseurs en secours, bascule automatique"
is for. If every provider's limit is still tighter than the request within
RPC_TIMEOUT_S, call() raises its ordinary RpcTimeout, which the engine
turns into a free 504 - no special-casing needed here for that path.
MAX_BLOCK_RANGE only rejects requests that could never succeed on ANY known
provider, as a free, instant 422 before any RPC round trip.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, field_validator

from app.base_chain.registry import BaseRpcSpec, RpcComputeError, register
from app.base_chain.rpc_client import call, is_valid_address, is_valid_hash

MAX_BLOCK_RANGE = 2000
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def _check_range(from_block: int, to_block: int) -> None:
    if to_block < from_block:
        raise RpcComputeError("invalid_block_range", "to_block must be >= from_block")
    if to_block - from_block > MAX_BLOCK_RANGE:
        raise RpcComputeError(
            "range_too_wide",
            f"block range spans {to_block - from_block} blocks; Base's public RPC caps eth_getLogs at {MAX_BLOCK_RANGE}",
        )


async def _get_logs(filter_obj: dict) -> list:
    return await call("eth_getLogs", [filter_obj])


# ------------------------------------------------------------ base/erc20-transfers
class Erc20TransfersInput(BaseModel):
    token: str
    from_block: int = Field(ge=0)
    to_block: int = Field(ge=0)

    @field_validator("token")
    @classmethod
    def _validate_token(cls, v):
        if not is_valid_address(v):
            raise ValueError("token must be a 0x-prefixed 20-byte hex string")
        return v


class TransferEvent(BaseModel):
    block_number: int
    transaction_hash: str
    log_index: int
    from_address: str
    to_address: str
    value_raw: str


class Erc20TransfersOutput(BaseModel):
    token: str
    from_block: int
    to_block: int
    transfers: list[TransferEvent]


async def compute_erc20_transfers(inp: Erc20TransfersInput) -> Erc20TransfersOutput:
    _check_range(inp.from_block, inp.to_block)
    raw_logs = await _get_logs({
        "address": inp.token, "topics": [TRANSFER_TOPIC],
        "fromBlock": hex(inp.from_block), "toBlock": hex(inp.to_block),
    })
    transfers = []
    for lg in raw_logs:
        topics = lg["topics"]
        if len(topics) < 3:
            continue  # non-standard Transfer-topic log (indexed args differ) - skip, don't crash
        transfers.append(TransferEvent(
            block_number=int(lg["blockNumber"], 16),
            transaction_hash=lg["transactionHash"],
            log_index=int(lg["logIndex"], 16),
            from_address="0x" + topics[1][-40:],
            to_address="0x" + topics[2][-40:],
            value_raw=str(int(lg["data"], 16)) if lg["data"] not in ("0x", "0x0") else "0",
        ))
    return Erc20TransfersOutput(token=inp.token, from_block=inp.from_block, to_block=inp.to_block, transfers=transfers)


register(BaseRpcSpec(
    slug="base/erc20-transfers", price="$0.005", service_name="base-erc20-transfers",
    description="ERC20 Transfer event logs for one token over a Base mainnet block range (max 2,000 blocks) via eth_getLogs.",
    tags=["erc20 transfers base", "transfer events", "base rpc", "eth_getLogs", "token activity"],
    input_model=Erc20TransfersInput, output_model=Erc20TransfersOutput, compute=compute_erc20_transfers,
    sample_input={"token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "from_block": 52118214, "to_block": 52118214},
    sample_output={
        "token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "from_block": 52118214, "to_block": 52118214,
        "transfers": [{
            "block_number": 52118214, "transaction_hash": "0xc2490a8a0aedd1196617a0e52111f82d6059986db7f1713ab23221c91c42f5c4",
            "log_index": 234, "from_address": "0x3cedc3cba49c3809ee46b9bf60da75d6607b45ec",
            "to_address": "0xb3f32bdfe8d07825bc0d7387295ab1d7559ba69d", "value_raw": "1000",
        }],
    },
))


# ------------------------------------------------------------------- base/events
class EventsInput(BaseModel):
    address: Optional[str] = None
    topics: list[Optional[str]] = Field(default_factory=list, max_length=4)
    from_block: int = Field(ge=0)
    to_block: int = Field(ge=0)

    @field_validator("address")
    @classmethod
    def _validate_address(cls, v):
        if v is not None and not is_valid_address(v):
            raise ValueError("address must be a 0x-prefixed 20-byte hex string")
        return v

    @field_validator("topics")
    @classmethod
    def _validate_topics(cls, v):
        for topic in v:
            if topic is not None and not is_valid_hash(topic):
                raise ValueError("each topic must be null or a 0x-prefixed 32-byte hex string")
        return v


class LogEntry(BaseModel):
    address: str
    topics: list[str]
    data: str
    block_number: int
    transaction_hash: str
    log_index: int


class EventsOutput(BaseModel):
    address: Optional[str]
    topics: list[Optional[str]]
    from_block: int
    to_block: int
    logs: list[LogEntry]


async def compute_events(inp: EventsInput) -> EventsOutput:
    _check_range(inp.from_block, inp.to_block)
    filter_obj = {"fromBlock": hex(inp.from_block), "toBlock": hex(inp.to_block)}
    if inp.address:
        filter_obj["address"] = inp.address
    if inp.topics:
        filter_obj["topics"] = inp.topics
    raw_logs = await _get_logs(filter_obj)
    logs = [
        LogEntry(
            address=lg["address"], topics=lg["topics"], data=lg["data"],
            block_number=int(lg["blockNumber"], 16),
            transaction_hash=lg["transactionHash"], log_index=int(lg["logIndex"], 16),
        )
        for lg in raw_logs
    ]
    return EventsOutput(address=inp.address, topics=inp.topics, from_block=inp.from_block, to_block=inp.to_block, logs=logs)


register(BaseRpcSpec(
    slug="base/events", price="$0.005", service_name="base-events",
    description="Generic Base mainnet event log search via eth_getLogs - filter by contract address and/or up to 4 topics over a block range (max 2,000 blocks).",
    tags=["base rpc", "event logs base", "eth_getLogs", "filter logs", "base mainnet"],
    input_model=EventsInput, output_model=EventsOutput, compute=compute_events,
    sample_input={
        "address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        "topics": ["0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"],
        "from_block": 52118214, "to_block": 52118214,
    },
    sample_output={
        "address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        "topics": ["0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"],
        "from_block": 52118214, "to_block": 52118214,
        "logs": [{
            "address": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
            "topics": [
                "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
                "0x0000000000000000000000003cedc3cba49c3809ee46b9bf60da75d6607b45ec",
                "0x000000000000000000000000b3f32bdfe8d07825bc0d7387295ab1d7559ba69d",
            ],
            "data": "0x00000000000000000000000000000000000000000000000000000000000003e8",
            "block_number": 52118214, "transaction_hash": "0xc2490a8a0aedd1196617a0e52111f82d6059986db7f1713ab23221c91c42f5c4",
            "log_index": 234,
        }],
    },
))
