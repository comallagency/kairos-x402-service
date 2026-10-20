"""Register AgentIndex as an autonomous ClawGig worker without leaking keys."""

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

DATA = Path("/app/data")
WALLET = DATA / "clawgig_solana_seed"
CREDS = DATA / "clawgig_credentials.json"


def b58(data: bytes) -> str:
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    number = int.from_bytes(data, "big")
    encoded = ""
    while number:
        number, remainder = divmod(number, 58)
        encoded = alphabet[remainder] + encoded
    return alphabet[0] * (len(data) - len(data.lstrip(b"\0"))) + (encoded or alphabet[0])


def request(path: str, body=None, api_key: str | None = None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Accept": "application/json", "User-Agent": "AgentIndex/1.0"}
    if data:
        headers["Content-Type"] = "application/json"
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(
        "https://clawgig.ai" + path,
        data=data,
        headers=headers,
        method="POST" if data else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"error": raw[:1000]}
        return exc.code, payload


if WALLET.exists():
    seed = bytes.fromhex(WALLET.read_text().strip())
else:
    seed = os.urandom(32)
    WALLET.write_text(seed.hex())
    WALLET.chmod(0o600)

private_key = Ed25519PrivateKey.from_private_bytes(seed)
public_key = private_key.public_key().public_bytes(
    serialization.Encoding.Raw,
    serialization.PublicFormat.Raw,
)
wallet = b58(public_key)

if CREDS.exists():
    credentials = json.loads(CREDS.read_text())
else:
    message = f"Register AgentIndex on ClawGig: {int(time.time())}"
    status, result = request(
        "/api/v1/agents/register/autonomous",
        {
            "name": "AgentIndex",
            "username": "agentindex-x402",
            "description": (
                "Autonomous Python, TypeScript, API documentation, web research, "
                "security review and data analysis worker powered by Cursor Composer."
            ),
            "skills": [
                "python", "typescript", "fastapi", "research",
                "api-documentation", "security-review", "data-analysis",
            ],
            "categories": ["code", "research", "writing", "data"],
            "webhook_url": "https://x402.agentindex.world/clawgig/webhook",
            "avatar_url": "https://x402.agentindex.world/favicon.ico",
            "contact_email": "comallagency@gmail.com",
            "solana_wallet": wallet,
            "wallet_signature": b58(private_key.sign(message.encode())),
            "wallet_message": message,
        },
    )
    if status not in (200, 201):
        raise RuntimeError(f"registration failed: {status} {result}")
    credentials = result
    CREDS.write_text(json.dumps(credentials))
    CREDS.chmod(0o600)

api_key = credentials.get("api_key")
status, gigs = request("/api/v1/gigs?limit=50", api_key=api_key)
rows = gigs.get("data", gigs.get("gigs", [])) if isinstance(gigs, dict) else []
print(json.dumps({
    "registered": bool(api_key),
    "wallet": wallet,
    "gigs_status": status,
    "gigs_count": len(rows) if isinstance(rows, list) else 0,
    "gigs": rows[:20] if isinstance(rows, list) else [],
    "error": gigs if status >= 400 else None,
}, ensure_ascii=False))
