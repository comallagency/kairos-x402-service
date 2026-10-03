"""base/contract, base/proxy - contract introspection. See
app/base_chain/registry.py.

No "ABI si verifie" field here despite the original brief mentioning it:
that requires a block explorer's source-verification index (Basescan's
API), not raw RPC - out of scope for a pack whose explicit requirement is
"RPC public Base uniquement... AUCUNE API payante". What raw RPC can answer
honestly: is it a contract, how big is its bytecode, does it expose a
standard owner() (best-effort - plenty of contracts have no owner by
design, that's a fact about the contract, not a failure), and is it an
EIP-1967/1967-beacon/EIP-1822 proxy.

EIP-1967 slot constants verified by direct keccak computation, not copied
from memory, then confirmed against a real proxy found on-chain (2026-10-03):
Compound's cUSDC v3 proxy on Base, 0xb125e6687d4313864e53df431d5425969c15eb2f,
whose implementation slot and admin slot both resolve to real, non-zero
addresses - and against USDC (0x833589...), confirmed NOT a proxy (slot is
zero), as the negative case.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, field_validator

from app.base_chain.registry import BaseRpcSpec, register
from app.base_chain.rpc_client import (
    call,
    decode_address,
    gather,
    is_valid_address,
    multicall,
    selector,
)

_OWNER_SEL = selector("owner()")
_UUPS_PROXIABLE_SEL = selector("proxiableUUID()")

# EIP-1967 storage slots: keccak256("eip1967.proxy.<name>") - 1, and the
# EIP-1822 (UUPS) PROXIABLE slot - canonical, identical on every EVM chain.
_EIP1967_IMPLEMENTATION_SLOT = "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"
_EIP1967_BEACON_SLOT = "0xa3f0ad74e5423aebfd80d3ef4346578335a9a72aeaee59ff6cb3582b35133d50"
_EIP1967_ADMIN_SLOT = "0xb53127684a568b3173ae13b9f8a6016e243e63b6e8ee1178d6a717850b5d6103"
_EIP1822_PROXIABLE_SLOT = "0xc5f16f0fcc639fa48a6947836d9850f504798523bf8c9a3a87d5876cf622bcf7"


def _check_address(v: str) -> str:
    if not is_valid_address(v):
        raise ValueError("must be a 0x-prefixed 20-byte hex string")
    return v


class AddressInput(BaseModel):
    address: str

    @field_validator("address")
    @classmethod
    def _validate(cls, v):
        return _check_address(v)


async def _detect_proxy(address: str) -> dict:
    # The 3 slots are independent reads - fetched concurrently (one round
    # trip's worth of latency, not three stacked sequentially) since at
    # most one of them is ever actually set on a real proxy. Found this
    # mattered after benchmarking compute_contract() below: eth_getCode +
    # owner() + a sequential 3-slot scan averaged ~886ms (p95 up to 3s) on
    # a plain non-proxy contract like USDC, which must always walk all 3
    # slots since none of them hit - now concurrent throughout.
    impl_raw, beacon_raw, uups_raw = await gather(
        call("eth_getStorageAt", [address, _EIP1967_IMPLEMENTATION_SLOT, "latest"]),
        call("eth_getStorageAt", [address, _EIP1967_BEACON_SLOT, "latest"]),
        call("eth_getStorageAt", [address, _EIP1822_PROXIABLE_SLOT, "latest"]),
    )
    implementation = decode_address(bytes.fromhex(impl_raw[2:]))
    if implementation:
        return {"is_proxy": True, "proxy_type": "eip1967", "implementation": implementation}

    beacon = decode_address(bytes.fromhex(beacon_raw[2:]))
    if beacon:
        return {"is_proxy": True, "proxy_type": "eip1967-beacon", "implementation": None}

    if int(uups_raw, 16) != 0:
        # EIP-1822's PROXIABLE slot convention isn't as uniform as EIP-1967's
        # for what it stores - flagged as a UUPS-style proxy without
        # guessing at an implementation address from it (untested against a
        # real example on Base, unlike the EIP-1967 path above).
        return {"is_proxy": True, "proxy_type": "eip1822", "implementation": None}

    return {"is_proxy": False, "proxy_type": "none", "implementation": None}


# ------------------------------------------------------------------ base/contract
class ContractOutput(BaseModel):
    address: str
    is_contract: bool
    bytecode_size: int
    owner: Optional[str]
    is_proxy: bool


async def compute_contract(inp: AddressInput) -> ContractOutput:
    # code, owner(), and the proxy-slot scan are independent of each other -
    # run concurrently rather than one-after-another (same reasoning as
    # _detect_proxy above: this used to be up to 5 sequential round trips).
    code, owner_results, proxy_info = await gather(
        call("eth_getCode", [inp.address, "latest"]),
        multicall([(inp.address, True, _OWNER_SEL)]),
        _detect_proxy(inp.address),
    )
    if not isinstance(code, str) or code in ("0x", "0x0"):
        return ContractOutput(address=inp.address, is_contract=False, bytecode_size=0, owner=None, is_proxy=False)
    bytecode_size = max(len(code) // 2 - 1, 0)

    owner_ok, owner_data = owner_results[0]
    owner = decode_address(owner_data) if owner_ok else None

    return ContractOutput(
        address=inp.address, is_contract=True, bytecode_size=bytecode_size,
        owner=owner, is_proxy=proxy_info["is_proxy"],
    )


register(BaseRpcSpec(
    slug="base/contract", price="$0.005", service_name="base-contract",
    description="Contract introspection on Base mainnet - is it a contract, bytecode size, best-effort owner(), and whether it's a proxy.",
    tags=["base rpc", "contract introspection", "owner lookup", "base mainnet", "smart contract info"],
    input_model=AddressInput, output_model=ContractOutput, compute=compute_contract,
    sample_input={"address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"},
    sample_output={
        "address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "is_contract": True, "bytecode_size": 1852,
        "owner": "0x3abd6f64a422225e61e435bae41db12096106df7", "is_proxy": False,
    },
))


# --------------------------------------------------------------------- base/proxy
class ProxyOutput(BaseModel):
    address: str
    is_proxy: bool
    proxy_type: str
    implementation: Optional[str]


async def compute_proxy(inp: AddressInput) -> ProxyOutput:
    info = await _detect_proxy(inp.address)
    return ProxyOutput(address=inp.address, **info)


register(BaseRpcSpec(
    slug="base/proxy", price="$0.005", service_name="base-proxy",
    description="Is this Base contract an upgradeable proxy? Proxy contract detection for EIP-1967 (transparent/UUPS), beacon, or EIP-1822 patterns, returning the implementation address.",
    tags=["base rpc", "proxy detection", "eip-1967", "upgradeable contract", "base mainnet"],
    input_model=AddressInput, output_model=ProxyOutput, compute=compute_proxy,
    sample_input={"address": "0xb125e6687d4313864e53df431d5425969c15eb2f"},
    sample_output={
        "address": "0xb125e6687d4313864e53df431d5425969c15eb2f", "is_proxy": True,
        "proxy_type": "eip1967", "implementation": "0xc1455ae6df6cd808ed677f048e434e22892682a7",
    },
))
