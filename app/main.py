"""Application FastAPI — kit x402 payant uniquement.

Surfaces de discours public (accueil, place, mesh, trust-kit, contact, etc.)
retirées : seuls les services payants USDC + découverte technique minimale.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastmcp.utilities.lifespan import combine_lifespans

from app import db
from app.admin import router as admin_router
from app.capacity import CapacityGateMiddleware, JobsCircuitBreakerMiddleware, LLMGatewayCircuitBreakerMiddleware, PinnedModelCircuitBreakerMiddleware
from app.client_ip import ClientIpMiddleware
from app.db import init_db
from app.discovery import router as discovery_router
from app.generated.dynamic_routes import build_dynamic_routers
from app.handlers.can_pay import router as can_pay_router
from app.handlers.capabilities import router as capabilities_router
from app.handlers.crypto import router as crypto_router
from app.handlers.discover_paid import router as discover_paid_router
from app.handlers.discover_paid import warm_discover_cache
from app.handlers.extract import router as extract_router
from app.handlers.fact_check import router as fact_check_router
from app.handlers.agent_health import router as agent_health_router
from app.handlers.agent_claim import router as agent_claim_router
from app.handlers.gas_price import router as gas_price_router
from app.handlers.jobs import router as jobs_router
from app.handlers.jev import router as jev_router
from app.handlers.llm_gateway import router as llm_gateway_router
from app.handlers.news import router as news_router
from app.handlers.pdf import router as pdf_router
from app.handlers.probe import router as probe_router
from app.handlers.search import router as search_router
from app.handlers.summarize import router as summarize_router
from app.handlers.token_risk import router as token_risk_router
from app.handlers.research import router as research_router
from app.handlers.sentiment import router as sentiment_router
from app.handlers.classify import router as classify_router
from app.handlers.intent import router as intent_router
from app.handlers.spam_check import router as spam_check_router
from app.handlers.toxicity import router as toxicity_router
from app.handlers.language import router as language_router
from app.handlers.pii_check import router as pii_check_router
from app.handlers.llm_claude_sonnet import router as llm_claude_sonnet_router
from app.handlers.llm_gpt_mini import router as llm_gpt_mini_router
from app.handlers.llm_gemini_flash import router as llm_gemini_flash_router
from app.handlers.llm_llama import router as llm_llama_router
from app.handlers.llm_deepseek import router as llm_deepseek_router
from app.handlers.translate import router as translate_router
from app.handlers.weather import router as weather_router
from app.handlers.wallet_balance import router as wallet_balance_router
from app.handlers.wallet_intelligence import router as wallet_intelligence_router
from app.handlers.x402_echo import router as x402_echo_router
from app.handlers.web_read import router as web_read_router
from app.intent_logging import IntentLoggingMiddleware
from app.jobs_worker import worker_loop
from app.mcp_accept_compat import McpAcceptCompatMiddleware
from app.mcp_server import mcp
from app.mpp_middleware import MPPMiddleware
from app.marketplace_worker import marketplace_loop
from app.openapi_custom import build_custom_openapi
from app.payment_body_compat import PaymentBodyCompatMiddleware
from app.x402_setup import KIT_TAGLINE, build_payment_middleware

logger = logging.getLogger("x402.main")

init_db()


async def heartbeat_loop(interval_seconds: float = 60.0):
    while True:
        try:
            db.record_heartbeat(True)
        except Exception:
            logger.exception("heartbeat write failed")
        await asyncio.sleep(interval_seconds)


@asynccontextmanager
async def app_lifespan(app: FastAPI):
    await warm_discover_cache()
    tasks = [
        asyncio.create_task(worker_loop()),
        asyncio.create_task(heartbeat_loop()),
        asyncio.create_task(marketplace_loop()),
    ]
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass


mcp_app = mcp.http_app(path="/")
lifespan = combine_lifespans(app_lifespan, mcp_app.lifespan)

inner_app = FastAPI(
    title="AgentIndex x402",
    description=(
        f"{KIT_TAGLINE} Pay-per-call tools for AI agents in USDC on Base "
        "(x402/MPP): search, pdf, web-read, extract, summarize, fact-check, "
        "translate, jobs, discover. See GET /capabilities."
    ),
    version="1.1.0",
    contact={"email": "comallagency@gmail.com"},
    lifespan=lifespan,
)

inner_app.include_router(translate_router)
inner_app.include_router(jobs_router)
inner_app.include_router(search_router)
inner_app.include_router(fact_check_router)
inner_app.include_router(pdf_router)
inner_app.include_router(web_read_router)
inner_app.include_router(extract_router)
inner_app.include_router(summarize_router)
inner_app.include_router(token_risk_router)
inner_app.include_router(research_router)
inner_app.include_router(sentiment_router)
inner_app.include_router(classify_router)
inner_app.include_router(intent_router)
inner_app.include_router(spam_check_router)
inner_app.include_router(toxicity_router)
inner_app.include_router(language_router)
inner_app.include_router(pii_check_router)
inner_app.include_router(llm_claude_sonnet_router)
inner_app.include_router(llm_gpt_mini_router)
inner_app.include_router(llm_gemini_flash_router)
inner_app.include_router(llm_llama_router)
inner_app.include_router(llm_deepseek_router)
inner_app.include_router(discover_paid_router)
inner_app.include_router(weather_router)
inner_app.include_router(crypto_router)
inner_app.include_router(news_router)
inner_app.include_router(can_pay_router)
inner_app.include_router(probe_router)
inner_app.include_router(wallet_balance_router)
inner_app.include_router(gas_price_router)
inner_app.include_router(wallet_intelligence_router)
inner_app.include_router(x402_echo_router)
inner_app.include_router(agent_health_router)
inner_app.include_router(agent_claim_router)
inner_app.include_router(jev_router)
inner_app.include_router(llm_gateway_router)
inner_app.include_router(capabilities_router)
inner_app.include_router(discovery_router)
inner_app.include_router(admin_router)
for _generated_router in build_dynamic_routers():
    inner_app.include_router(_generated_router)
inner_app.openapi = build_custom_openapi(inner_app)

inner_app.mount("/mcp", mcp_app)


@inner_app.get("/health", openapi_extra={"security": []})
async def health():
    return {"status": "ok"}


FAVICON_PATH = Path(__file__).parent / "static" / "favicon.ico"
ICON_PATH = Path(__file__).parent / "static" / "icon.png"


@inner_app.get("/favicon.ico", openapi_extra={"security": []})
async def favicon():
    return FileResponse(FAVICON_PATH, media_type="image/x-icon")


@inner_app.get("/icon.png", openapi_extra={"security": []})
async def icon():
    return FileResponse(ICON_PATH, media_type="image/png")


mcp_accept_compat = McpAcceptCompatMiddleware(inner_app)
payment_wrapped = build_payment_middleware(mcp_accept_compat)
intent_logged = IntentLoggingMiddleware(payment_wrapped)
mpp_wrapped = MPPMiddleware(intent_logged, inner_app)
capacity_gated = CapacityGateMiddleware(mpp_wrapped)
jobs_breaker = JobsCircuitBreakerMiddleware(capacity_gated)
llm_gateway_breaker = LLMGatewayCircuitBreakerMiddleware(jobs_breaker)
pinned_model_breaker = PinnedModelCircuitBreakerMiddleware(llm_gateway_breaker)
body_compat = PaymentBodyCompatMiddleware(pinned_model_breaker)
# Outermost: captures the client IP (already resolved from X-Forwarded-For
# by uvicorn proxy-headers) before anything else runs, purely observational.
app = ClientIpMiddleware(body_compat)
