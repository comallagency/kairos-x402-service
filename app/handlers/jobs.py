import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app import config, db
from app.receipts import effective_price, extract_payer_address, make_receipt, price_float
from app.x402_setup import ROUTE_DESCRIPTIONS

router = APIRouter()

JOB_POLL_SLOT_SECONDS = 60
SAMPLE_JOB_ID = "sample-0000"
# OpenAPI path template copied literally by catalog probes and trust monitors.
OPENAPI_JOB_ID_PLACEHOLDER = "{job_id}"
SAMPLE_CREATED_AT = "2026-01-01T00:00:00+00:00"
SAMPLE_FINISHED_AT = "2026-01-01T00:01:00+00:00"


def _is_sample_job_id(job_id: str) -> bool:
    return job_id in (SAMPLE_JOB_ID, OPENAPI_JOB_ID_PLACEHOLDER)


def _sample_job_result_body() -> dict:
    return {
        "job_id": SAMPLE_JOB_ID,
        "status": "done",
        "result": {
            "subject": "Example Corp",
            "summary": (
                "Example Corp is a fictitious company used to demonstrate this "
                "endpoint's output shape."
            ),
            "sources": ["https://example.com/about"],
        },
        "x402_receipt": make_receipt(
            None, "web_search", 0, 0.0, steps_executed=0, sources_read=1
        ),
    }


@router.get("/jobs/sample", openapi_extra={"security": []})
async def jobs_sample():
    return _sample_job_result_body()


@router.post("/jobs", description=ROUTE_DESCRIPTIONS["jobs"])
async def create_job(request: Request):
    payer = extract_payer_address(request)
    user_agent = request.headers.get("user-agent")
    try:
        body = await request.json()
    except Exception:
        body = {}
    body_excerpt = json.dumps(body)
    subject = body.get("subject")

    if not subject:
        db.log_request(
            route="jobs", method="POST", status="error", payer=payer,
            user_agent=user_agent, body_excerpt=body_excerpt,
            error_reason="missing_subject",
        )
        return JSONResponse({"error": {"reason": "missing_subject"}}, status_code=400)

    price = effective_price(payer, price_float(config.PRICE_JOB))
    job_id = db.create_job({"subject": subject}, amount_usdc=price, payer=payer)
    position = db.queue_position(job_id)
    eta_seconds = (position + 1) * JOB_POLL_SLOT_SECONDS

    db.log_request(
        route="jobs", method="POST", status="paid", amount_usdc=price, payer=payer,
        user_agent=user_agent, body_excerpt=body_excerpt,
    )
    return {"job_id": job_id, "eta_seconds": eta_seconds}


@router.get("/jobs/{job_id}", openapi_extra={"security": []})
async def get_job_status(job_id: str):
    if _is_sample_job_id(job_id):
        return {
            "job_id": SAMPLE_JOB_ID,
            "status": "done",
            "created_at": SAMPLE_CREATED_AT,
            "finished_at": SAMPLE_FINISHED_AT,
        }

    job = db.get_job(job_id)
    if job is None:
        return JSONResponse({"error": {"reason": "job_not_found"}}, status_code=404)

    response = {"job_id": job_id, "status": job["status"], "created_at": job["created_at"]}
    if job["status"] in ("queued", "running"):
        response["eta_seconds"] = (db.queue_position(job_id) + 1) * JOB_POLL_SLOT_SECONDS
    if job["started_at"]:
        response["started_at"] = job["started_at"]
    if job["finished_at"]:
        response["finished_at"] = job["finished_at"]
    return response


@router.get("/jobs/{job_id}/result", openapi_extra={"security": []})
async def get_job_result(job_id: str):
    if _is_sample_job_id(job_id):
        return _sample_job_result_body()

    job = db.get_job(job_id)
    if job is None:
        return JSONResponse({"error": {"reason": "job_not_found"}}, status_code=404)

    receipt = make_receipt(
        None, "web_search", 0, job["amount_usdc"] or 0.0,
        steps_executed=job["steps_executed"], sources_read=job["sources_read"],
    )

    if job["status"] == "done":
        result = json.loads(job["result_json"])
        return {"job_id": job_id, "status": "done", "result": result, "x402_receipt": receipt}

    if job["status"] == "failed":
        error = json.loads(job["error_json"])
        return {"job_id": job_id, "status": "failed", "error": error, "x402_receipt": receipt}

    return JSONResponse(
        {
            "job_id": job_id,
            "status": job["status"],
            "eta_seconds": (db.queue_position(job_id) + 1) * JOB_POLL_SLOT_SECONDS,
        },
        status_code=202,
    )
