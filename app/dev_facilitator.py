import logging

from x402 import SettleResponse, SupportedKind, SupportedResponse, VerifyResponse

logger = logging.getLogger("x402.dev_facilitator")


class LocalDevFacilitatorClient:
    """Stand-in facilitator used ONLY when CDP_API_KEY_ID/CDP_API_KEY_SECRET are
    not configured (local dev / unit tests). It answers get_supported() so the
    payment middleware can build a valid 402 challenge, but never verifies or
    settles a real payment -- verify()/settle() always fail closed.

    build_resource_server() only reaches for this in ENVIRONMENT != "production".
    """

    def __init__(self, network: str, scheme: str = "exact"):
        self._network = network
        self._scheme = scheme
        logger.warning(
            "x402: LocalDevFacilitatorClient active (no CDP keys configured). "
            "No real payment will ever verify or settle. Do not use in production."
        )

    def get_supported(self) -> SupportedResponse:
        return SupportedResponse(
            kinds=[SupportedKind(x402Version=2, scheme=self._scheme, network=self._network)]
        )

    async def verify(self, payload, requirements) -> VerifyResponse:
        return VerifyResponse(isValid=False, invalidReason="dev_facilitator_no_real_verification")

    async def settle(self, payload, requirements) -> SettleResponse:
        return SettleResponse(
            success=False,
            errorReason="dev_facilitator_no_real_settlement",
            transaction="",
            network=self._network,
        )
