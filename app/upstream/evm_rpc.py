"""Small resilient EVM JSON-RPC client for read-only agent utilities."""

from __future__ import annotations

from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class EvmNetwork:
    key: str
    caip2: str
    name: str
    native_symbol: str
    rpc_urls: tuple[str, ...]
    usdc: str


NETWORKS = {
    "base": EvmNetwork(
        key="base",
        caip2="eip155:8453",
        name="Base",
        native_symbol="ETH",
        rpc_urls=(
            "https://1rpc.io/base",
            "https://base.drpc.org",
            "https://mainnet.base.org",
        ),
        usdc="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    ),
    "ethereum": EvmNetwork(
        key="ethereum",
        caip2="eip155:1",
        name="Ethereum",
        native_symbol="ETH",
        rpc_urls=(
            "https://ethereum-rpc.publicnode.com",
            "https://eth.drpc.org",
        ),
        usdc="0xA0b86991c6218b36c1d19d4a2e9Eb0cE3606eB48",
    ),
    "polygon": EvmNetwork(
        key="polygon",
        caip2="eip155:137",
        name="Polygon",
        native_symbol="POL",
        rpc_urls=(
            "https://polygon-bor-rpc.publicnode.com",
            "https://polygon.drpc.org",
        ),
        usdc="0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359",
    ),
    "arbitrum": EvmNetwork(
        key="arbitrum",
        caip2="eip155:42161",
        name="Arbitrum One",
        native_symbol="ETH",
        rpc_urls=(
            "https://arbitrum-one-rpc.publicnode.com",
            "https://arbitrum.drpc.org",
        ),
        usdc="0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
    ),
    "optimism": EvmNetwork(
        key="optimism",
        caip2="eip155:10",
        name="Optimism",
        native_symbol="ETH",
        rpc_urls=(
            "https://optimism-rpc.publicnode.com",
            "https://optimism.drpc.org",
        ),
        usdc="0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85",
    ),
}

_ALIASES = {
    "1": "ethereum",
    "eth": "ethereum",
    "mainnet": "ethereum",
    "eip155:1": "ethereum",
    "8453": "base",
    "eip155:8453": "base",
    "137": "polygon",
    "matic": "polygon",
    "pol": "polygon",
    "eip155:137": "polygon",
    "42161": "arbitrum",
    "arb": "arbitrum",
    "arbitrum-one": "arbitrum",
    "eip155:42161": "arbitrum",
    "10": "optimism",
    "op": "optimism",
    "eip155:10": "optimism",
}


class EvmRpcError(Exception):
    pass


def resolve_network(value: object) -> EvmNetwork:
    raw = str(value or "base").strip().lower()
    key = _ALIASES.get(raw, raw)
    network = NETWORKS.get(key)
    if network is None:
        raise EvmRpcError("unsupported_network")
    return network


async def rpc(network: EvmNetwork, method: str, params: list):
    last_error: Exception | None = None
    async with httpx.AsyncClient(timeout=12.0) as client:
        for url in network.rpc_urls:
            try:
                response = await client.post(
                    url,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": method,
                        "params": params,
                    },
                )
                response.raise_for_status()
                payload = response.json()
                if payload.get("error"):
                    raise EvmRpcError("rpc_error")
                if "result" not in payload:
                    raise EvmRpcError("rpc_invalid_response")
                return payload["result"]
            except (httpx.HTTPError, ValueError, EvmRpcError) as exc:
                last_error = exc
    raise EvmRpcError("rpc_unavailable") from last_error
