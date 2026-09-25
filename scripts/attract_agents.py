#!/usr/bin/env python3
"""Re-hit free agent-discovery surfaces + print the Hermes/OpenClaw attraction process.

Hermes / OpenClaw / PipRail do NOT wander and pay random 402s. They discover via:
  1. CDP Bazaar (needs first settle)
  2. Open indexes (402index, agent402, PipRail discover)
  3. Skills / MCP config (skills/agentindex-x402)
  4. /llms.txt + agent.json + openapi
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

BASE = "https://x402.agentindex.world"
ROUTES = [
    ("can-pay", f"{BASE}/can-pay"),
    ("probe", f"{BASE}/probe"),
    ("weather", f"{BASE}/weather"),
    ("crypto", f"{BASE}/crypto"),
    ("news", f"{BASE}/news"),
    ("search", f"{BASE}/search"),
    ("pdf-to-markdown", f"{BASE}/pdf"),
    ("web-read", f"{BASE}/web-read"),
    ("structured-extract", f"{BASE}/extract"),
    ("summarize", f"{BASE}/summarize"),
    ("fact-check", f"{BASE}/fact-check"),
    ("wallet-balance", f"{BASE}/wallet-balance"),
    ("gas-price", f"{BASE}/gas-price"),
    ("wallet-intelligence", f"{BASE}/wallet-intelligence"),
    ("x402-echo", f"{BASE}/x402-echo"),
    ("agent-health", f"{BASE}/agent-health"),
    ("tip", f"{BASE}/tip"),
    ("agent-claim", f"{BASE}/agent-claim"),
]

ROUTE_META = {
    "can-pay": ("GET", 0.001, "infrastructure", "Check Base USDC balance before an agent pays."),
    "probe": ("GET", 0.001, "infrastructure", "Inspect an x402 paywall, price, network, asset and payTo."),
    "weather": ("GET", 0.001, "data", "Current global weather and a 3-day forecast by city or coordinates."),
    "crypto": ("GET", 0.001, "data", "Live crypto spot prices and 24-hour changes."),
    "news": ("GET", 0.001, "data", "Current Hacker News headlines as clean JSON."),
    "search": ("POST", 0.0001, "search", "Live web search plus clean full-page Markdown from the top 3 results."),
    "pdf-to-markdown": ("POST", 0.002, "documents", "Extract real PDF text to Markdown with page metadata."),
    "web-read": ("POST", 0.002, "documents", "Read a URL and return clean Markdown."),
    "structured-extract": ("POST", 0.003, "data", "Extract webpage content into a caller-provided JSON schema."),
    "summarize": ("POST", 0.003, "documents", "Summarize URL, text or HTML into concise JSON."),
    "fact-check": ("POST", 0.01, "research", "Verify a claim against live web sources with citations."),
    "wallet-balance": ("GET", 0.0001, "infrastructure", "Read native coin and USDC balances across five EVM networks."),
    "gas-price": ("GET", 0.0001, "infrastructure", "Read live gas, base fee and transfer cost across five EVM networks."),
    "wallet-intelligence": ("GET", 0.001, "infrastructure", "Replace ten wallet and gas reads with one five-network x402 preflight."),
    "x402-echo": ("GET", 0.000001, "infrastructure", "Test a complete Base-mainnet x402 settlement for one atomic unit of USDC."),
    "agent-health": ("GET", 0.001, "infrastructure", "Verify agent uptime, latency, x402, OpenAPI, MCP and A2A discovery in one call."),
    "tip": ("GET", 0.01, "infrastructure", "Voluntary one-cent USDC support payment with an on-chain receipt."),
    "agent-claim": ("POST", 0.01, "infrastructure", "Pay 0.01 USDC for a 30-day verified agent listing and public score badge."),
}


def get(url: str) -> tuple[int, int]:
    req = urllib.request.Request(url, headers={"User-Agent": "AgentIndex-attract/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, len(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, 0
    except Exception:
        return 0, 0


def post_json(url: str, body: dict) -> tuple[int, str]:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "AgentIndex-attract/1.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")[:400]
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:400]
    except Exception as e:
        return 0, str(e)[:200]


def main() -> int:
    print("=== Surfaces ===")
    for path in ("/llms.txt", "/agent.json", "/capabilities", "/.well-known/x402"):
        code, n = get(BASE + path)
        print(f"  {code}  {path}  ({n} o)")

    print("\n=== 402index ===")
    for name, url in ROUTES:
        method, price, category, description = ROUTE_META[name]
        code, body = post_json(
            "https://402index.io/api/v1/register",
            {
                "url": url,
                "name": f"AgentIndex {name}",
                "protocol": "x402",
                "http_method": method,
                "price_usd": price,
                "payment_asset": "USDC",
                "payment_network": "base",
                "category": category,
                "provider": "AgentIndex",
                "description": f"{description} ${price:g} USDC on Base; no account or API key.",
            },
        )
        print(f"  {name}: {code} {body[:120]}")

    print("\n=== agent402 ===")
    code, body = post_json("https://agent402.tools/api/index/register", {"origin": BASE})
    print(f"  {code} {body[:200]}")

    print(
        """
=== Process Hermes / OpenClaw ===
1. Skill: skills/agentindex-x402/SKILL.md → hermes skills publish / ClawHub
2. MCP:  https://x402.agentindex.world/mcp/
3. PipRail: piprail_register(<our URLs>) depuis un Hermes avec @piprail/mcp
4. awesome-x402 PR (xpaysh/awesome-x402, Recall-Kitchen/awesome-x402-mcp-services)
5. Bazaar: $0.001 settle (scripts/bootstrap_bazaar_settle.py) → OpenClaw bazaar skill nous voit

Les agents paient seulement si: découverte + wallet + besoin. (1)(2)(4) = gratuit.
"""
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
