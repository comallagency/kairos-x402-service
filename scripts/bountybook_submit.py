"""Submit the first AgentIndex BountyBook deliverable.

The wallet key stays in /app/data/bountybook_wallet and is never logged.
"""

import json
import urllib.error
import urllib.request
from pathlib import Path

from eth_account import Account
from eth_account.messages import encode_defunct

BASE = "https://api.bountybook.ai"
JOB_ID = "740fd768-1dcb-410c-a7ad-c64ac8be50af"

CODE = '''"""Utilities for flattening nested dictionaries."""

from __future__ import annotations

from typing import Any


def flatten_dict(d: dict, sep: str = ".") -> dict:
    """Return *d* flattened with nested keys joined by *sep*.

    Dictionaries are traversed recursively. All other values, including lists
    and ``None``, remain unchanged. Empty nested dictionaries contribute no
    output key.
    """
    result: dict[str, Any] = {}

    def visit(value: dict, prefix: str) -> None:
        for key, item in value.items():
            path = f"{prefix}{sep}{key}" if prefix else str(key)
            if isinstance(item, dict):
                visit(item, path)
            else:
                result[path] = item

    visit(d, "")
    return result
'''


def request(path: str, body: dict | None = None, token: str | None = None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Accept": "application/json", "User-Agent": "AgentIndex/1.0"}
    if data:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        BASE + path, data=data, headers=headers, method="POST" if data else "GET"
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"error": raw[:1000]}
        return exc.code, payload


private_key = Path("/app/data/bountybook_wallet").read_text().strip()
account = Account.from_key(private_key)
_, nonce = request(f"/auth/nonce?address={account.address.lower()}")
signature = Account.sign_message(
    encode_defunct(text=nonce["nonce"]), private_key
).signature.hex()
if not signature.startswith("0x"):
    signature = "0x" + signature
status, auth = request(
    "/auth/verify",
    {"address": account.address.lower(), "signature": signature},
)
if status != 200:
    raise RuntimeError(f"Authentication failed: {status} {auth}")

status, claim = request(
    f"/jobs/{JOB_ID}/claim",
    {"executorAddress": account.address},
    auth["token"],
)
if status not in (200, 409):
    raise RuntimeError(f"Claim failed: {status} {claim}")

deliverable = {
    "files": {"flatten.py": CODE},
    "flatten.py": CODE,
    "content": CODE,
    "code": CODE,
    "language": "python",
}
status, result = request(
    f"/jobs/{JOB_ID}/submit",
    {
        "executorAddress": account.address,
        "executor_address": account.address,
        # The public docs say camelCase, while current failed attempts indicate
        # an older snake_case verifier is still deployed. Send both.
        "outputData": deliverable,
        "output_data": deliverable,
        "output": deliverable,
    },
    auth["token"],
)
print(json.dumps({"address": account.address, "submit_status": status, "result": result}))
