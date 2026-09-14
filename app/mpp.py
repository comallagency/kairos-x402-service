"""Machine Payments Protocol (MPP) support - the "Payment" HTTP authentication
scheme (tempoxyz/mpp-specs), added ALONGSIDE the existing x402 flow, never
replacing it. Added on explicit instruction: MPP is a condition of entry into
AgentCash's mppscan index, not a payment rail we expect real volume on (see
BRIEF-CORRECTIONS.md).

Method chosen: "evm" / intent "charge" / credential type "authorization"
(specs/methods/evm/draft-evm-charge-00.md) - this is EIP-3009
`transferWithAuthorization`, the exact same primitive our x402 "exact" scheme
already uses for USDC on Base. Verification is pure cryptography (no new
trust assumptions). Settlement submits the authorization on-chain via a
CDP-managed server account (get_or_create_account) - never a raw private key
on this server, consistent with the rest of this project.

Everything price/asset/network-related is derived from build_route_configs()
via the caller, never hand-copied.
"""

import base64
import json
import time
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from hmac import HMAC, compare_digest
from urllib.parse import urlparse

from eth_abi import encode as abi_encode
from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils import keccak, to_checksum_address

from app import config

MPP_METHOD = "evm"
MPP_INTENT = "charge"
CHALLENGE_TTL_SECONDS = 300

_TRANSFER_WITH_AUTH_SELECTOR = keccak(
    text="transferWithAuthorization(address,address,uint256,uint256,uint256,bytes32,uint8,bytes32,bytes32)"
)[:4]

_NETWORK_NAMES = {"eip155:8453": "base", "eip155:84532": "base-sepolia"}


class MPPError(Exception):
    """Credential rejected - reason is a spec error code from
    draft-httpauth-payment-00's Error Codes table."""

    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    padded = s + "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(padded)


def _jcs(obj) -> str:
    """Minimal JSON Canonicalization Scheme (RFC 8785) for the flat,
    float-free objects this module produces: recursively sort object keys,
    no whitespace. Sufficient for our payloads (strings/ints/nested objects,
    no floats) without pulling in a full JCS implementation."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _realm() -> str:
    return urlparse(config.BASE_URL).netloc


def _chain_id() -> int:
    return int(config.X402_NETWORK.split(":", 1)[1])


def _cdp_network_name() -> str:
    return _NETWORK_NAMES.get(config.X402_NETWORK, "base")


def _challenge_id(realm: str, method: str, intent: str, request_b64url: str, expires: str) -> str:
    """HMAC-SHA256 stateless challenge binding, exactly per
    draft-httpauth-payment-00's "Recommended: HMAC-SHA256 Binding" - digest,
    opaque and header are never used here, so those slots are always empty."""
    input_str = "|".join([realm, method, intent, request_b64url, expires, "", ""])
    mac = HMAC(config.MPP_CHALLENGE_SECRET.encode(), input_str.encode(), sha256).digest()
    return _b64url_encode(mac)


def build_www_authenticate(price_atomic: str, asset_address: str, pay_to: str, description: str) -> str:
    """Build one `WWW-Authenticate: Payment ...` challenge value for the
    given route's price/asset/recipient (already resolved to on-chain atomic
    units and a checksummed address by the caller - see
    app/mpp_middleware.py)."""
    realm = _realm()
    expires = (datetime.now(timezone.utc) + timedelta(seconds=CHALLENGE_TTL_SECONDS)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    request_obj = {
        "amount": price_atomic,
        "currency": asset_address,
        "recipient": pay_to,
        "methodDetails": {"chainId": _chain_id(), "credentialTypes": ["authorization"]},
    }
    request_b64url = _b64url_encode(_jcs(request_obj).encode())
    challenge_id = _challenge_id(realm, MPP_METHOD, MPP_INTENT, request_b64url, expires)

    parts = [
        f'id="{challenge_id}"',
        f'realm="{realm}"',
        f'method="{MPP_METHOD}"',
        f'intent="{MPP_INTENT}"',
        f'expires="{expires}"',
        f'description="{description[:120]}"',
        f'request="{request_b64url}"',
    ]
    return "Payment " + ", ".join(parts)


def parse_credential(authorization_header: str) -> dict:
    """Decode an `Authorization: Payment <b64url>` (or `Payment-Authorization`)
    header value into the credential JSON object. Raises MPPError on any
    malformed input - never returns a partial/best-effort result."""
    prefix = "payment "
    if not authorization_header or not authorization_header.strip().lower().startswith(prefix):
        raise MPPError("malformed-credential", "Expected 'Payment <base64url>'")
    token = authorization_header.strip()[len(prefix):].strip()
    try:
        decoded = _b64url_decode(token)
        credential = json.loads(decoded)
    except Exception as exc:
        raise MPPError("malformed-credential", f"Invalid base64url/JSON: {exc}") from exc
    if not isinstance(credential, dict) or "challenge" not in credential or "payload" not in credential:
        raise MPPError("malformed-credential", "Credential missing 'challenge' or 'payload'")
    return credential


def verify_credential(credential: dict, *, expected_amount: str, expected_asset: str, expected_recipient: str) -> dict:
    """Full verification per draft-evm-charge-00's Authorization Verification
    section (signature, binding, amount, recipient, expiry) - everything
    EXCEPT the on-chain balance check, which only the chain itself can
    answer at settlement time. Returns the validated payload dict
    (from/to/value/nonce/...) on success. Raises MPPError otherwise."""
    challenge = credential.get("challenge") or {}
    payload = credential.get("payload") or {}

    for field in ("id", "realm", "method", "intent", "request", "expires"):
        if not challenge.get(field):
            raise MPPError("malformed-credential", f"challenge.{field} missing")

    expected_id = _challenge_id(
        challenge["realm"], challenge["method"], challenge["intent"], challenge["request"], challenge["expires"]
    )
    if not compare_digest(expected_id, challenge["id"]):
        raise MPPError("invalid-challenge", "Challenge id does not match its own bound parameters")

    try:
        expires_at = datetime.strptime(challenge["expires"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise MPPError("malformed-credential", f"Bad expires timestamp: {exc}") from exc
    if datetime.now(timezone.utc) > expires_at:
        raise MPPError("payment-expired", "Challenge has expired")

    try:
        request_obj = json.loads(_b64url_decode(challenge["request"]))
    except Exception as exc:
        raise MPPError("malformed-credential", f"Invalid challenge.request: {exc}") from exc

    if request_obj.get("currency", "").lower() != expected_asset.lower():
        raise MPPError("invalid-challenge", "Challenge currency does not match this resource")
    if request_obj.get("recipient", "").lower() != expected_recipient.lower():
        raise MPPError("invalid-challenge", "Challenge recipient does not match this resource")
    if request_obj.get("amount") != expected_amount:
        raise MPPError("payment-insufficient", "Challenge amount does not match this resource's price")

    if payload.get("type") != "authorization":
        raise MPPError("method-unsupported", "Only credential type 'authorization' is accepted")

    for field in ("from", "to", "value", "validAfter", "validBefore", "nonce", "signature"):
        if payload.get(field) in (None, ""):
            raise MPPError("malformed-credential", f"payload.{field} missing")

    expected_nonce = "0x" + keccak(text=challenge["id"] + challenge["realm"]).hex()
    if payload["nonce"].lower() != expected_nonce.lower():
        raise MPPError("verification-failed", "nonce is not bound to this challenge")
    if payload["to"].lower() != expected_recipient.lower():
        raise MPPError("verification-failed", "payload.to does not match challenge recipient")
    if payload["value"] != expected_amount:
        raise MPPError("verification-failed", "payload.value does not match challenge amount")
    if int(payload["validBefore"]) < int(time.time()):
        raise MPPError("payment-expired", "Authorization validBefore has passed")

    asset = request_obj  # currency/chainId already validated above
    domain = {
        "name": _asset_name(),
        "version": _asset_version(),
        "chainId": request_obj["methodDetails"]["chainId"],
        "verifyingContract": to_checksum_address(asset["currency"]),
    }
    message_types = {
        "TransferWithAuthorization": [
            {"name": "from", "type": "address"},
            {"name": "to", "type": "address"},
            {"name": "value", "type": "uint256"},
            {"name": "validAfter", "type": "uint256"},
            {"name": "validBefore", "type": "uint256"},
            {"name": "nonce", "type": "bytes32"},
        ]
    }
    message = {
        "from": to_checksum_address(payload["from"]),
        "to": to_checksum_address(payload["to"]),
        "value": int(payload["value"]),
        "validAfter": int(payload["validAfter"]),
        "validBefore": int(payload["validBefore"]),
        "nonce": bytes.fromhex(payload["nonce"][2:]),
    }
    signable = encode_typed_data(domain_data=domain, message_types=message_types, message_data=message)
    try:
        recovered = Account.recover_message(signable, signature=payload["signature"])
    except Exception as exc:
        raise MPPError("verification-failed", f"Signature recovery failed: {exc}") from exc
    if recovered.lower() != payload["from"].lower():
        raise MPPError("verification-failed", "Signature does not recover to payload.from")

    return {
        "from": to_checksum_address(payload["from"]),
        "to": to_checksum_address(payload["to"]),
        "value": int(payload["value"]),
        "validAfter": int(payload["validAfter"]),
        "validBefore": int(payload["validBefore"]),
        "nonce": payload["nonce"],
        "signature": payload["signature"],
    }


def _asset_name() -> str:
    from x402.mechanisms.evm.default_assets import get_default_asset

    return get_default_asset(config.X402_NETWORK)["name"]


def _asset_version() -> str:
    from x402.mechanisms.evm.default_assets import get_default_asset

    return get_default_asset(config.X402_NETWORK)["version"]


def _split_signature(signature: str) -> tuple[int, bytes, bytes]:
    sig = bytes.fromhex(signature[2:])
    if len(sig) != 65:
        raise MPPError("malformed-credential", f"signature must be 65 bytes, got {len(sig)}")
    r, s, v = sig[:32], sig[32:64], sig[64]
    if v < 27:
        v += 27
    return v, r, s


async def settle_credential(verified: dict, asset_address: str) -> str:
    """Submit the verified EIP-3009 authorization on-chain via a CDP-managed
    server account (get_or_create_account - CDP holds the key, this process
    never does) and wait for a confirmed receipt. Returns the transaction
    hash. Raises MPPError (verification-failed) on any on-chain failure -
    never fabricates a receipt."""
    from cdp import CdpClient
    from cdp.evm_transaction_types import TransactionRequestEIP1559

    v, r, s = _split_signature(verified["signature"])
    calldata = _TRANSFER_WITH_AUTH_SELECTOR + abi_encode(
        ["address", "address", "uint256", "uint256", "uint256", "bytes32", "uint8", "bytes32", "bytes32"],
        [
            verified["from"],
            verified["to"],
            verified["value"],
            verified["validAfter"],
            verified["validBefore"],
            bytes.fromhex(verified["nonce"][2:]),
            v,
            r,
            s,
        ],
    )

    cdp = CdpClient(
        api_key_id=config.CDP_API_KEY_ID,
        api_key_secret=config.CDP_API_KEY_SECRET,
        wallet_secret=config.CDP_WALLET_SECRET,
    )
    try:
        account = await cdp.evm.get_or_create_account(name=config.MPP_SETTLEMENT_ACCOUNT_NAME)
        tx_hash = await cdp.evm.send_transaction(
            address=account.address,
            transaction=TransactionRequestEIP1559(to=to_checksum_address(asset_address), data=calldata, value=0),
            network=_cdp_network_name(),
        )
    except Exception as exc:
        raise MPPError("verification-failed", f"On-chain settlement failed: {exc}") from exc
    finally:
        await cdp.close()

    return tx_hash


def build_receipt(tx_hash: str) -> str:
    """`Payment-Receipt` header value per draft-httpauth-payment-00 /
    draft-evm-charge-00 - `status` is only ever "success", matching the
    spec's requirement that receipts are never issued on failure."""
    receipt = {
        "status": "success",
        "method": MPP_METHOD,
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "reference": tx_hash,
        "chainId": _chain_id(),
    }
    return _b64url_encode(_jcs(receipt).encode())
