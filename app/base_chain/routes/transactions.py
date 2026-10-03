"""base/tx, base/receipt - transaction lookup by hash. See
app/base_chain/registry.py."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, field_validator

from app.base_chain.registry import BaseRpcSpec, RpcComputeError, register
from app.base_chain.rpc_client import call, is_valid_hash


class HashInput(BaseModel):
    hash: str

    @field_validator("hash")
    @classmethod
    def _validate_hash(cls, v):
        if not is_valid_hash(v):
            raise ValueError("hash must be a 0x-prefixed 32-byte hex string")
        return v


# ---------------------------------------------------------------------- base/tx
class TxOutput(BaseModel):
    hash: str
    block_number: Optional[int] = None
    from_address: str
    to_address: Optional[str] = None
    value_wei: int
    input_data: str
    gas: int
    gas_price_wei: Optional[int] = None
    nonce: int


async def compute_tx(inp: HashInput) -> TxOutput:
    raw = await call("eth_getTransactionByHash", [inp.hash])
    if raw is None:
        raise RpcComputeError("tx_not_found", "no transaction matches that hash")
    return TxOutput(
        hash=raw["hash"],
        block_number=int(raw["blockNumber"], 16) if raw.get("blockNumber") else None,
        from_address=raw["from"],
        to_address=raw.get("to"),
        value_wei=int(raw["value"], 16),
        input_data=raw.get("input", "0x"),
        gas=int(raw["gas"], 16),
        gas_price_wei=int(raw["gasPrice"], 16) if raw.get("gasPrice") else None,
        nonce=int(raw["nonce"], 16),
    )


register(BaseRpcSpec(
    slug="base/tx", price="$0.008", service_name="base-tx",
    description="Full Base mainnet transaction details by hash via eth_getTransactionByHash - from, to, value, calldata, gas, block.",
    tags=["base rpc", "transaction", "base mainnet", "eth_getTransactionByHash", "tx lookup"],
    input_model=HashInput, output_model=TxOutput, compute=compute_tx,
    sample_input={"hash": "0xc2490a8a0aedd1196617a0e52111f82d6059986db7f1713ab23221c91c42f5c4"},
    sample_output={
        "hash": "0xc2490a8a0aedd1196617a0e52111f82d6059986db7f1713ab23221c91c42f5c4",
        "block_number": 52118214, "from_address": "0x59b7ebc67a3d627fabaf06768c818638452ae704",
        "to_address": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913", "value_wei": 0,
        "input_data": "0xe3ee160e0000000000000000000000003cedc3cba49c3809ee46b9bf60da75d6607b45ec000000000000000000000000b3f32bdfe8d07825bc0d7387295ab1d7559ba69d00000000000000000000000000000000000000000000000000000000000003e8",
        "gas": 95905, "gas_price_wei": 5025000, "nonce": 1579520,
    },
))


# ---------------------------------------------------------------- base/receipt
class ReceiptLog(BaseModel):
    address: str
    topics: list[str]
    data: str
    log_index: int


class ReceiptOutput(BaseModel):
    hash: str
    block_number: int
    status: bool
    gas_used: int
    contract_address: Optional[str] = None
    logs: list[ReceiptLog]


async def compute_receipt(inp: HashInput) -> ReceiptOutput:
    raw = await call("eth_getTransactionReceipt", [inp.hash])
    if raw is None:
        raise RpcComputeError("receipt_not_found", "no receipt for that hash yet - unmined or unknown")
    logs = [
        ReceiptLog(
            address=lg["address"], topics=lg["topics"], data=lg["data"],
            log_index=int(lg["logIndex"], 16),
        )
        for lg in raw.get("logs") or []
    ]
    return ReceiptOutput(
        hash=raw["transactionHash"],
        block_number=int(raw["blockNumber"], 16),
        status=raw.get("status") == "0x1",
        gas_used=int(raw["gasUsed"], 16),
        contract_address=raw.get("contractAddress"),
        logs=logs,
    )


register(BaseRpcSpec(
    slug="base/receipt", price="$0.005", service_name="base-receipt",
    description="Transaction receipt via eth_getTransactionReceipt - status, gas used, and event logs for a mined Base mainnet transaction.",
    tags=["base rpc", "transaction receipt", "base mainnet", "eth_getTransactionReceipt", "tx status"],
    input_model=HashInput, output_model=ReceiptOutput, compute=compute_receipt,
    sample_input={"hash": "0xc2490a8a0aedd1196617a0e52111f82d6059986db7f1713ab23221c91c42f5c4"},
    sample_output={
        "hash": "0xc2490a8a0aedd1196617a0e52111f82d6059986db7f1713ab23221c91c42f5c4",
        "block_number": 52118214, "status": True, "gas_used": 86262, "contract_address": None,
        "logs": [
            {
                "address": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
                "topics": [
                    "0x98de503528ee59b575ef0c0a2576a82497bfc029a5685b209e9ec333479b10a5",
                    "0x0000000000000000000000003cedc3cba49c3809ee46b9bf60da75d6607b45ec",
                    "0xb0b24ee42f54b8fe2a1cb6032417c7c63a92867031c51404a9c97f6c0280501f",
                ],
                "data": "0x", "log_index": 233,
            },
            {
                "address": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
                "topics": [
                    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
                    "0x0000000000000000000000003cedc3cba49c3809ee46b9bf60da75d6607b45ec",
                    "0x000000000000000000000000b3f32bdfe8d07825bc0d7387295ab1d7559ba69d",
                ],
                "data": "0x00000000000000000000000000000000000000000000000000000000000003e8", "log_index": 234,
            },
        ],
    },
))
