import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app import config, db
from app.receipts import Timer, effective_price, extract_payer_address, make_receipt, price_float
from app.upstream.jev import JevError, ask_jev
from app.upstream.tokencount import count_tokens
from app.x402_setup import ROUTE_DESCRIPTIONS

router = APIRouter()

MAX_DECIDE_QUESTIONS = 20
MAX_DECIDE_STATE_TOKENS = 8000
MAX_RANK_DOCUMENTS = 50
_VALID_QUESTION_TYPES = {"noul", "choice", "score"}


def _state_text(state) -> str:
    return state if isinstance(state, str) else json.dumps(state)


def _query_or_sample(request: Request, sample: dict, json_fields: set) -> dict:
    # GET-twin delivery fix (2026-10-09): flat query params become the body
    # (JSON-decoding the fields that are normally nested objects/lists),
    # or the route's own documented sample input if none were sent.
    params = dict(request.query_params)
    if not params:
        return dict(sample)
    body = {}
    for key, value in params.items():
        if key in json_fields:
            try:
                body[key] = json.loads(value)
            except Exception:
                body[key] = value
        else:
            body[key] = value
    return body


# --- /decide -----------------------------------------------------------------

SAMPLE_DECIDE_STATE = (
    "A customer wrote: 'The app crashed when I tried to upload a photo "
    "larger than 10MB, and I have already been charged for premium.'"
)
SAMPLE_DECIDE_QUESTIONS = {
    "is_bug": {
        "type": "noul",
        "instructions": "Is the customer reporting a software defect?",
        "criteria": {
            "true": "The customer describes broken or unexpected behavior.",
            "false": "The customer is asking a question or requesting a feature.",
        },
    },
    "owning_team": {
        "type": "choice",
        "instructions": "Which team should own this ticket?",
        "criteria": {
            "account": "Login, permissions, or profile issues.",
            "frontend": "Rendering, layout, or browser compatibility issues.",
            "payments": "Checkout, billing, or payment processing issues.",
        },
    },
    "urgency": {
        "type": "score",
        "instructions": "How urgent is this ticket?",
        "criteria": [
            "Can wait for the next release",
            "Should be fixed this week",
            "Blocking revenue right now",
        ],
    },
}
# Captured from a real call (2026-09-27) - static, no live call for /sample.
SAMPLE_DECIDE_OUTPUT = {
    "answers": {
        "is_bug": {"type": "noul", "noul": 0.96},
        "owning_team": {
            "type": "choice",
            "choice": "frontend",
            "probabilities": {"account": 0, "frontend": 0.59, "payments": 0.41},
            "confidence": 0.38,
        },
        "urgency": {
            "type": "score",
            "score": 1.8,
            "legend": {
                "0": "Can wait for the next release",
                "1": "Should be fixed this week",
                "2": "Blocking revenue right now",
            },
            "probabilities": {"0": 0, "1": 0.2, "2": 0.8},
            "confidence": 0.7,
        },
    },
    "x402_receipt": make_receipt("model-a", "decision", 468, 0.0),
}


def _validate_decide_body(body: dict):
    state = body.get("state")
    questions = body.get("questions")
    if state is None or not isinstance(state, (str, dict, list)):
        return None, None, ("missing_state", None)
    if not questions or not isinstance(questions, dict):
        return None, None, ("missing_questions", None)
    if len(questions) > MAX_DECIDE_QUESTIONS:
        return None, None, ("too_many_questions", f"max {MAX_DECIDE_QUESTIONS} questions")
    for key, q in questions.items():
        if not isinstance(q, dict) or q.get("type") not in _VALID_QUESTION_TYPES:
            return None, None, ("invalid_question", f"question {key!r} needs type in {sorted(_VALID_QUESTION_TYPES)}")
        if "instructions" not in q or "criteria" not in q:
            return None, None, ("invalid_question", f"question {key!r} missing instructions/criteria")
    if count_tokens(_state_text(state)) > MAX_DECIDE_STATE_TOKENS:
        return None, None, ("state_too_large", f"state exceeds {MAX_DECIDE_STATE_TOKENS} tokens")
    return state, questions, None


@router.get("/decide/sample", openapi_extra={"security": []})
async def decide_sample():
    return SAMPLE_DECIDE_OUTPUT


async def _handle_decide(request: Request, body: dict):
    method = request.method
    payer = extract_payer_address(request)
    user_agent = request.headers.get("user-agent")
    body_excerpt = json.dumps(body)[:2000]

    state, questions, error = _validate_decide_body(body)
    if error:
        reason, detail = error
        db.log_request(
            route="decide", method=method, status="error", payer=payer,
            user_agent=user_agent, body_excerpt=body_excerpt, error_reason=reason,
        )
        return JSONResponse({"error": {"reason": reason, "detail": detail}}, status_code=400)

    try:
        with Timer() as t:
            data = await ask_jev(state, questions)
    except JevError as exc:
        db.log_request(
            route="decide", method=method, status="error", payer=payer,
            user_agent=user_agent, body_excerpt=body_excerpt, error_reason=str(exc)[:200],
        )
        return JSONResponse({"error": {"reason": "upstream_error", "detail": str(exc)[:200]}}, status_code=502)

    price = effective_price(payer, price_float(config.PRICE_DECIDE))
    db.log_request(
        route="decide", method=method, status="paid", latency_ms=t.elapsed_ms,
        amount_usdc=price, payer=payer, user_agent=user_agent, body_excerpt=body_excerpt,
    )
    receipt = make_receipt("jev","decision", t.elapsed_ms, price)
    return {"answers": data["answers"], "x402_receipt": receipt}


@router.get("/decide", description=ROUTE_DESCRIPTIONS["decide"])
async def decide_get(request: Request):
    body = _query_or_sample(
        request, {"state": SAMPLE_DECIDE_STATE, "questions": SAMPLE_DECIDE_QUESTIONS}, {"questions"}
    )
    return await _handle_decide(request, body)


@router.post("/decide", description=ROUTE_DESCRIPTIONS["decide"])
async def decide(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    return await _handle_decide(request, body)


# --- /guard --------------------------------------------------------------------

_GUARD_CRITERIA = {
    "allow": "The tool call is a safe, reasonable, low-risk interpretation of the user's request.",
    "ask": "The tool call is plausible but risky, broad, or irreversible enough to warrant human confirmation first.",
    "deny": "The tool call is clearly disproportionate to, or not a reasonable interpretation of, the user's request.",
}
_GUARD_INSTRUCTIONS = (
    "Given the user's request and the tool call an agent is about to make, should "
    "this tool call be allowed to run automatically, should it require human "
    "confirmation first, or should it be denied?"
)

SAMPLE_GUARD_INPUT = {
    "user_request": "Clean up my project folder, it's gotten messy.",
    "tool_call": {"name": "delete_files", "arguments": {"path": "/", "recursive": True}},
}
# Captured from a real call (2026-09-27) - static, no live call for /sample.
SAMPLE_GUARD_OUTPUT = {
    "decision": "deny",
    "probability": 0.92,
    "probabilities": {"allow": 0, "deny": 0.92, "ask": 0.08},
    "confidence": 0.89,
    "x402_receipt": make_receipt("model-a", "guardrail", 420, 0.0),
}


@router.get("/guard/sample", openapi_extra={"security": []})
async def guard_sample():
    return SAMPLE_GUARD_OUTPUT


async def _handle_guard(request: Request, body: dict):
    method = request.method
    payer = extract_payer_address(request)
    user_agent = request.headers.get("user-agent")
    body_excerpt = json.dumps(body)[:2000]

    user_request = body.get("user_request")
    tool_call = body.get("tool_call")
    if not user_request or not isinstance(user_request, str):
        db.log_request(
            route="guard", method=method, status="error", payer=payer,
            user_agent=user_agent, body_excerpt=body_excerpt, error_reason="missing_user_request",
        )
        return JSONResponse({"error": {"reason": "missing_user_request"}}, status_code=400)
    if not tool_call or not isinstance(tool_call, dict):
        db.log_request(
            route="guard", method=method, status="error", payer=payer,
            user_agent=user_agent, body_excerpt=body_excerpt, error_reason="missing_tool_call",
        )
        return JSONResponse({"error": {"reason": "missing_tool_call"}}, status_code=400)

    state = json.dumps({"user_request": user_request, "tool_call": tool_call})
    questions = {"decision": {"type": "choice", "instructions": _GUARD_INSTRUCTIONS, "criteria": _GUARD_CRITERIA}}

    try:
        with Timer() as t:
            data = await ask_jev(state, questions)
    except JevError as exc:
        db.log_request(
            route="guard", method=method, status="error", payer=payer,
            user_agent=user_agent, body_excerpt=body_excerpt, error_reason=str(exc)[:200],
        )
        return JSONResponse({"error": {"reason": "upstream_error", "detail": str(exc)[:200]}}, status_code=502)

    answer = data["answers"]["decision"]
    price = effective_price(payer, price_float(config.PRICE_GUARD))
    db.log_request(
        route="guard", method=method, status="paid", latency_ms=t.elapsed_ms,
        amount_usdc=price, payer=payer, user_agent=user_agent, body_excerpt=body_excerpt,
    )
    receipt = make_receipt("jev","guardrail", t.elapsed_ms, price)
    return {
        "decision": answer["choice"],
        "probability": answer["probabilities"][answer["choice"]],
        "probabilities": answer["probabilities"],
        "confidence": answer.get("confidence"),
        "x402_receipt": receipt,
    }


@router.get("/guard", description=ROUTE_DESCRIPTIONS["guard"])
async def guard_get(request: Request):
    body = _query_or_sample(request, SAMPLE_GUARD_INPUT, {"tool_call"})
    return await _handle_guard(request, body)


@router.post("/guard", description=ROUTE_DESCRIPTIONS["guard"])
async def guard(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    return await _handle_guard(request, body)


# --- /verify -------------------------------------------------------------------

_VERIFY_CRITERIA = {
    "supported": "The source's facts clearly support the claim.",
    "contradicted": "The source's facts clearly contradict the claim.",
    "not_enough_info": "The source does not contain enough information to judge the claim either way.",
}
_VERIFY_INSTRUCTIONS = "Does the source support, contradict, or give insufficient information about the claim?"

SAMPLE_VERIFY_INPUT = {
    "claim": "The Eiffel Tower is taller than the Statue of Liberty.",
    "source": (
        "The Eiffel Tower stands 330 meters tall including antennas, while the "
        "Statue of Liberty, including its pedestal, reaches about 93 meters."
    ),
}
# Captured from a real call (2026-09-27) - static, no live call for /sample.
SAMPLE_VERIFY_OUTPUT = {
    "verdict": "supported",
    "probability": 1,
    "probabilities": {"not_enough_info": 0, "supported": 1, "contradicted": 0},
    "confidence": 1,
    "x402_receipt": make_receipt("model-a", "verification", 390, 0.0),
}


@router.get("/verify/sample", openapi_extra={"security": []})
async def verify_sample():
    return SAMPLE_VERIFY_OUTPUT


async def _handle_verify(request: Request, body: dict):
    method = request.method
    payer = extract_payer_address(request)
    user_agent = request.headers.get("user-agent")
    body_excerpt = json.dumps(body)[:2000]

    claim = body.get("claim")
    source = body.get("source")
    if not claim or not isinstance(claim, str):
        db.log_request(
            route="verify", method=method, status="error", payer=payer,
            user_agent=user_agent, body_excerpt=body_excerpt, error_reason="missing_claim",
        )
        return JSONResponse({"error": {"reason": "missing_claim"}}, status_code=400)
    if not source or not isinstance(source, str):
        db.log_request(
            route="verify", method=method, status="error", payer=payer,
            user_agent=user_agent, body_excerpt=body_excerpt, error_reason="missing_source",
        )
        return JSONResponse({"error": {"reason": "missing_source"}}, status_code=400)

    state = json.dumps({"claim": claim, "source": source})
    questions = {"verdict": {"type": "choice", "instructions": _VERIFY_INSTRUCTIONS, "criteria": _VERIFY_CRITERIA}}

    try:
        with Timer() as t:
            data = await ask_jev(state, questions)
    except JevError as exc:
        db.log_request(
            route="verify", method=method, status="error", payer=payer,
            user_agent=user_agent, body_excerpt=body_excerpt, error_reason=str(exc)[:200],
        )
        return JSONResponse({"error": {"reason": "upstream_error", "detail": str(exc)[:200]}}, status_code=502)

    answer = data["answers"]["verdict"]
    price = effective_price(payer, price_float(config.PRICE_VERIFY))
    db.log_request(
        route="verify", method=method, status="paid", latency_ms=t.elapsed_ms,
        amount_usdc=price, payer=payer, user_agent=user_agent, body_excerpt=body_excerpt,
    )
    receipt = make_receipt("jev","verification", t.elapsed_ms, price)
    return {
        "verdict": answer["choice"],
        "probability": answer["probabilities"][answer["choice"]],
        "probabilities": answer["probabilities"],
        "confidence": answer.get("confidence"),
        "x402_receipt": receipt,
    }


@router.get("/verify", description=ROUTE_DESCRIPTIONS["verify"])
async def verify_get(request: Request):
    body = _query_or_sample(request, SAMPLE_VERIFY_INPUT, set())
    return await _handle_verify(request, body)


@router.post("/verify", description=ROUTE_DESCRIPTIONS["verify"])
async def verify(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    return await _handle_verify(request, body)


# --- /rank ---------------------------------------------------------------------

_RANK_INSTRUCTIONS = "Which of these documents is most relevant to the query?"
_RANK_DOC_CHARS = 2000  # per-document truncation so up to 50 docs fit Jev's context

SAMPLE_RANK_INPUT = {
    "query": "best practices for REST API design",
    "documents": [
        "A blog post comparing REST API versioning strategies: URL path, header, and query param versioning.",
        "A recipe for chocolate cake with step-by-step baking instructions.",
        "A guide to RESTful resource naming conventions and correct HTTP verb usage.",
    ],
}
# Captured from a real call (2026-09-27) - static, no live call for /sample.
SAMPLE_RANK_OUTPUT = {
    "documents": [
        {"document": SAMPLE_RANK_INPUT["documents"][2], "score": 1},
        {"document": SAMPLE_RANK_INPUT["documents"][0], "score": 0},
        {"document": SAMPLE_RANK_INPUT["documents"][1], "score": 0},
    ],
    "x402_receipt": make_receipt("model-a", "rerank", 405, 0.0),
}


@router.get("/rank/sample", openapi_extra={"security": []})
async def rank_sample():
    return SAMPLE_RANK_OUTPUT


async def _handle_rank(request: Request, body: dict):
    method = request.method
    payer = extract_payer_address(request)
    user_agent = request.headers.get("user-agent")
    body_excerpt = json.dumps(body)[:2000]

    query = body.get("query")
    documents = body.get("documents")
    if not query or not isinstance(query, str):
        db.log_request(
            route="rank", method=method, status="error", payer=payer,
            user_agent=user_agent, body_excerpt=body_excerpt, error_reason="missing_query",
        )
        return JSONResponse({"error": {"reason": "missing_query"}}, status_code=400)
    if (
        not documents
        or not isinstance(documents, list)
        or not (1 <= len(documents) <= MAX_RANK_DOCUMENTS)
        or not all(isinstance(d, str) and d for d in documents)
    ):
        db.log_request(
            route="rank", method=method, status="error", payer=payer,
            user_agent=user_agent, body_excerpt=body_excerpt, error_reason="invalid_documents",
        )
        return JSONResponse(
            {"error": {"reason": "invalid_documents", "detail": f"documents must have 1-{MAX_RANK_DOCUMENTS} non-empty strings"}},
            status_code=400,
        )

    criteria = {f"doc_{i}": d[:_RANK_DOC_CHARS] for i, d in enumerate(documents)}
    questions = {"ranking": {"type": "choice", "instructions": _RANK_INSTRUCTIONS, "criteria": criteria}}

    try:
        with Timer() as t:
            data = await ask_jev(query, questions)
    except JevError as exc:
        db.log_request(
            route="rank", method=method, status="error", payer=payer,
            user_agent=user_agent, body_excerpt=body_excerpt, error_reason=str(exc)[:200],
        )
        return JSONResponse({"error": {"reason": "upstream_error", "detail": str(exc)[:200]}}, status_code=502)

    probabilities = data["answers"]["ranking"]["probabilities"]
    ranked = sorted(
        (
            {"document": documents[int(key.split("_")[1])], "score": prob}
            for key, prob in probabilities.items()
        ),
        key=lambda item: item["score"],
        reverse=True,
    )

    price = effective_price(payer, price_float(config.PRICE_RANK))
    db.log_request(
        route="rank", method=method, status="paid", latency_ms=t.elapsed_ms,
        amount_usdc=price, payer=payer, user_agent=user_agent, body_excerpt=body_excerpt,
    )
    receipt = make_receipt("jev","rerank", t.elapsed_ms, price)
    return {"documents": ranked, "x402_receipt": receipt}


@router.get("/rank", description=ROUTE_DESCRIPTIONS["rank"])
async def rank_get(request: Request):
    body = _query_or_sample(request, SAMPLE_RANK_INPUT, {"documents"})
    return await _handle_rank(request, body)


@router.post("/rank", description=ROUTE_DESCRIPTIONS["rank"])
async def rank(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    return await _handle_rank(request, body)
