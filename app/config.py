import os
import secrets
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ENVIRONMENT = os.getenv("ENVIRONMENT", "development")

X402_PAY_TO = os.getenv("X402_PAY_TO", "0x0000000000000000000000000000000000000000")
X402_NETWORK = os.getenv("X402_NETWORK", "eip155:84532")

CDP_API_KEY_ID = os.getenv("CDP_API_KEY_ID") or None
CDP_API_KEY_SECRET = os.getenv("CDP_API_KEY_SECRET") or None
# Separate from the API key pair above - required only for CDP-managed server
# wallets (used by MPP settlement, app/mpp.py) to sign/send transactions.
CDP_WALLET_SECRET = os.getenv("CDP_WALLET_SECRET") or None

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")

# Gemma3:4b local (systemd ollama, VPS) - juge /fact-check sans passer
# par OpenRouter (voir app/upstream/ollama.py). host.docker.internal:
# resolu vers l'hote via extra_hosts dans docker-compose.yml.
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://host.docker.internal:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "gemma3:4b")

# Local SearXNG instance, same docker network (docker-compose.yml) - no key,
# no per-call cost, independent of the OpenRouter account balance.
SEARXNG_URL = os.getenv("SEARXNG_URL", "http://searxng-kairos:8080")

MECHANICAL_WALLETS = os.getenv("MECHANICAL_WALLETS", "")

ADMIN_BASIC_AUTH_USER = os.getenv("ADMIN_BASIC_AUTH_USER", "admin")
ADMIN_BASIC_AUTH_PASS = os.getenv("ADMIN_BASIC_AUTH_PASS", "changeme")

# Authenticates usine/pc_listener.py (a machine, not a browser) against
# GET/POST /admin/usine/pc-request* - deliberately NOT Basic Auth, and
# deliberately NOT auto-generated like MPP_CHALLENGE_SECRET below: an
# unset value must fail loudly (see check_pc_token in app/admin.py), never
# silently mint a token the PC's own copy could never match.
PC_LISTENER_TOKEN = os.getenv("PC_LISTENER_TOKEN") or None

# Off by default: a brand-new wallet's first payment is exactly the event that
# triggers Bazaar CDP indexing and is the only revenue automated probes ever
# produce, so waiving it costs real indexing/revenue for a conversion perk
# that only matters once we're already discoverable. Flip to "true" once the
# first real settlement has landed and indexing is confirmed - see
# BRIEF-CORRECTIONS.md.
FREE_FIRST_CALL = os.getenv("FREE_FIRST_CALL", "false").strip().lower() == "true"

# MPP ("Payment" HTTP auth scheme, tempoxyz/mpp-specs) - added alongside x402,
# never replacing it. MPP_CHALLENGE_SECRET signs the stateless HMAC binding
# between a 402 challenge and the credential that redeems it; falling back to
# a random per-process secret is safe (a container restart just makes
# in-flight challenges fail closed and the client retries with a fresh one)
# but SHOULD be set explicitly in production so challenges survive restarts.
MPP_CHALLENGE_SECRET = os.getenv("MPP_CHALLENGE_SECRET") or secrets.token_hex(32)
# CDP-managed server account name that pays gas and submits the
# transferWithAuthorization call - CDP holds the key, this process never
# does. Must be funded with a small amount of native gas token on the target
# network before real MPP settlements can succeed (see BRIEF-CORRECTIONS.md).
MPP_SETTLEMENT_ACCOUNT_NAME = os.getenv("MPP_SETTLEMENT_ACCOUNT_NAME", "mpp-settlement")

DATA_DIR = Path(os.getenv("DATA_DIR", "./data")).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "requests.db"

BASE_URL = os.getenv("BASE_URL", "http://localhost:8000")

# Clé ed25519 (base58) publiée sur /.well-known/brick-blue.json pour revendiquer
# la fiche brick.blue (BrickBlueBot a sonné 404 le 2026-09-17).
BRICK_BLUE_PUBLIC_KEY = os.getenv("BRICK_BLUE_PUBLIC_KEY") or None
# Public hash for https://402index.io domain claim (/.well-known/402index-verify.txt)
INDEX_402_VERIFY_HASH = os.getenv("INDEX_402_VERIFY_HASH") or None

PRICE_TRANSLATE = "$0.005"
PRICE_JOB = "$0.10"

# 2026-09-20 conversion pricing: crawler traffic was healthy (1,421 valid 402s)
# but no settlement. Public marketplace comparables put PDF extraction around
# $0.003 and web/search utilities around $0.001-$0.01. Keep trial prices at or
# below those comparables until the first independent buyers establish quality.
# 24h market-entry price: search is 42% of observed x402 demand. At $0.0001,
# this is 100x below Tavily's $0.01 endpoint while still using zero-cost local
# SearXNG/page retrieval. Keep until independent settlements unlock Bazaar,
# then restore the sustainable $0.002 price.
PRICE_SEARCH = "$0.0001"
PRICE_PDF = "$0.002"
PRICE_WEB_READ = "$0.002"
PRICE_EXTRACT = "$0.003"
PRICE_SUMMARIZE = "$0.003"
PRICE_FACT_CHECK = "$0.01"

# Re-wired 2026-09-16, after the first PRICE_DISCOVER = "$0.001" (13:29 on
# 2026-09-11) was written and never connected to anything - a run was cut by
# its turn ceiling between the constant and the wiring, /discover stayed free
# and nothing ever read it. This one is consumed in three places in the same
# build: _core_route_configs() (the x402 challenge), the well-known, and
# agent.json - the middleware enforces exactly what those three advertise.
# Distinct from the free GET /discover (app/handlers/discover.py), which
# keeps its price-less contract: POST is the guaranteed-semantic-ranking
# variant over Kairos's local snapshot.
PRICE_DISCOVER = "$0.001"
# Ultra-cheap commodity endpoints — same $0.001 bait as services that collect.
PRICE_WEATHER = "$0.001"
PRICE_CRYPTO = "$0.001"
PRICE_NEWS = "$0.001"
# Pre-payment checklist agents run before spending elsewhere — first-settle bait.
PRICE_CAN_PAY = "$0.001"
PRICE_PROBE = "$0.001"
# Acquisition routes: below the common $0.001 Bazaar floor so autonomous
# routers can cheaply establish the first independent settlement and quality.
PRICE_WALLET_BALANCE = "$0.0001"
PRICE_GAS_PRICE = "$0.0001"
# One signature replaces ten wallet/gas calls (five chains × two services).
PRICE_WALLET_INTELLIGENCE = "$0.001"
# Cheapest possible USDC settlement: one atomic unit. This converts SDK,
# wallet and facilitator test traffic into real mainnet payment proof.
PRICE_X402_ECHO = "$0.000001"
# Voluntary one-cent support payment with an on-chain receipt.
PRICE_TIP = "$0.01"
# 30-day audited listing and public verification badge.
PRICE_AGENT_CLAIM = "$0.01"
# Fresh operational + discovery audit, below $0.002-$0.005 competitors.
PRICE_AGENT_HEALTH = "$0.001"
# GET /discover is free by design too, and has no price constant. A
# PRICE_DISCOVER = "$0.001" sat here from 13:29 on 2026-09-11 until it was
# removed: a run was cut by its turn ceiling after writing the constant and
# before wiring it to anything. Nothing ever read it - build_route_configs()
# never saw it, no middleware, no OpenAPI entry - so /discover stayed free
# while the config said it had a price. The service's own agent-card calls
# /discover "free, no account, no payment"; the constant contradicted it in
# silence.
# GET /detect-language is free by design (the kit's entry point) - no price
# constant, it never goes through build_route_configs()/the payment
# middleware at all (see app/handlers/detect_language.py).

DAILY_CAPACITY = {
    "search": int(os.getenv("DAILY_CAPACITY_SEARCH", "500")),
    "translate": int(os.getenv("DAILY_CAPACITY_TRANSLATE", "1000")),
    "jobs": int(os.getenv("DAILY_CAPACITY_JOBS", "50")),
    "pdf": int(os.getenv("DAILY_CAPACITY_PDF", "1000")),
    "web-read": int(os.getenv("DAILY_CAPACITY_WEB_READ", "1000")),
    "extract": int(os.getenv("DAILY_CAPACITY_EXTRACT", "500")),
    "summarize": int(os.getenv("DAILY_CAPACITY_SUMMARIZE", "1000")),
    "fact-check": int(os.getenv("DAILY_CAPACITY_FACT_CHECK", "500")),
    "discover": int(os.getenv("DAILY_CAPACITY_DISCOVER", "500")),
    "weather": int(os.getenv("DAILY_CAPACITY_WEATHER", "2000")),
    "crypto": int(os.getenv("DAILY_CAPACITY_CRYPTO", "2000")),
    "news": int(os.getenv("DAILY_CAPACITY_NEWS", "2000")),
    "can-pay": int(os.getenv("DAILY_CAPACITY_CAN_PAY", "3000")),
    "probe": int(os.getenv("DAILY_CAPACITY_PROBE", "1500")),
    "wallet-balance": int(os.getenv("DAILY_CAPACITY_WALLET_BALANCE", "5000")),
    "gas-price": int(os.getenv("DAILY_CAPACITY_GAS_PRICE", "5000")),
    "wallet-intelligence": int(os.getenv("DAILY_CAPACITY_WALLET_INTELLIGENCE", "2000")),
    "x402-echo": int(os.getenv("DAILY_CAPACITY_X402_ECHO", "10000")),
    "tip": int(os.getenv("DAILY_CAPACITY_TIP", "10000")),
    "agent-claim": int(os.getenv("DAILY_CAPACITY_AGENT_CLAIM", "3000")),
    "agent-health": int(os.getenv("DAILY_CAPACITY_AGENT_HEALTH", "3000")),
}

# OpenRouter caps the "models" fallback array at 3 entries per request, so the
# 4th model from the brief ("openrouter/free") is tried as a separate last-resort
# call if all 3 primary models fail - see chat_completion_with_fallback().
#
# minimax-m2.7:free and minimax-m3:free replaced 2026-09-11: both now return
# 404 "This model is unavailable for free" (discontinued from the free tier,
# same failure mode as the z-ai/glm-5.2 entry they themselves replaced on
# 2026-09-06). google/gemma-4-31b-it:free, the third entry, is separately
# rate-limited (429) most of the time. Together this meant /summarize/sample
# (and every other LLM-backed route sharing this list) fell through to the
# slow last-resort call on nearly every request - 8/9 observed calls from the
# uptime monitor returned 502 over the last 9h (checked via nginx access log
# + reproducing the calls directly against OpenRouter from inside the
# container). Replaced with three models confirmed live and fast just now
# (GET /api/v1/models filtered to ":free", then each called with a real
# chat completion, not just a HEAD/list check - the list alone doesn't tell
# you what's actually serving):
OPENROUTER_TRANSLATE_MODELS = [
    "nex-agi/nex-n2.5-mini:free",
    "inclusionai/ling-3.0-flash-vl:free",
    "dots-studio/dots-3-note-preview:free",
]
OPENROUTER_LAST_RESORT_MODEL = "openrouter/free"

OPENROUTER_MAX_CALLS_PER_JOB = 20
OPENROUTER_MAX_SEARCHES_PER_JOB = 5

# Garde-fou de la boucle autonome (usine/loop_run.sh) : la clé OpenRouter du
# projet n'a pas de `limit` configuré côté OpenRouter (vu en interrogeant
# /api/v1/key - `limit`/`limit_remaining` sont `null`), donc il n'existe
# aucun signal natif de "quota restant en %". Ce budget journalier est notre
# propre seuil, comparé à `usage_daily` (dollars dépensés aujourd'hui,
# toutes routes confondues) - pas le compteur de requêtes gratuites
# par-modèle d'OpenRouter, qui n'est pas exposé par cette clé. $2 reprend
# l'objectif de départ du projet ("2$, puis 1000$/mois").
OPENROUTER_DAILY_BUDGET_USD = float(os.getenv("OPENROUTER_DAILY_BUDGET_USD", "2.00"))
