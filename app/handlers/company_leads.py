"""POST /company-leads - B2B lead search backed by Apify (b2bsearch/b2b-
leads-finder): give a sector and a country (2-letter code, optionally
narrowed by city), get back people currently working at matching
companies, each with their own email plus their company's name, domain
(when known) and employee count. Async, job-polling pattern (like POST
/jobs) - Apify runs are not fast enough for a synchronous response, see
app/company_leads_worker.py.
"""

from __future__ import annotations

import json
import re

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app import config, db
from app.receipts import effective_price, extract_payer_address, make_receipt, price_float
from app.x402_setup import ROUTE_DESCRIPTIONS

router = APIRouter()

COMPANY_LEADS_POLL_SLOT_SECONDS = 30
SAMPLE_JOB_ID = "sample-0000"
OPENAPI_JOB_ID_PLACEHOLDER = "{job_id}"
SAMPLE_CREATED_AT = "2026-01-01T00:00:00+00:00"
SAMPLE_FINISHED_AT = "2026-01-01T00:01:00+00:00"

_COUNTRY_RE = re.compile(r"^[a-zA-Z]{2}$")

SAMPLE_LEADS = [
    {
        "company_name": "Diduenjoy",
        "company_domain": "diduenjoy.com",
        "company_size": 34,
        "company_industry": "Software Development",
        "contact_name": "Alexandre Dupont",
        "contact_title": "Head of Growth",
        "email": "alexandre@diduenjoy.com",
        "email_type": "work",
        "linkedin_url": "https://www.linkedin.com/in/alexandre-example",
        "location": "Paris, Île-de-France, France",
    }
]


def _is_sample_job_id(job_id: str) -> bool:
    return job_id in (SAMPLE_JOB_ID, OPENAPI_JOB_ID_PLACEHOLDER)


def _sample_result_body() -> dict:
    return {
        "job_id": SAMPLE_JOB_ID,
        "status": "done",
        "result": {
            "query": {"sector": "Software Development", "country": "fr", "city": "Paris", "max_results": 25},
            "leads": SAMPLE_LEADS,
            "count": len(SAMPLE_LEADS),
        },
        "x402_receipt": make_receipt(None, "apify_b2b_leads", 0, 0.0, steps_executed=0),
    }


@router.get("/company-leads/sample", openapi_extra={"security": []})
async def company_leads_sample():
    return _sample_result_body()


@router.get("/company-leads/sample/result", openapi_extra={"security": []})
async def company_leads_sample_result():
    return _sample_result_body()


def _validate_query(body: dict) -> tuple[dict | None, str | None]:
    sector = str(body.get("sector") or "").strip()
    country = str(body.get("country") or "").strip().lower()
    city = str(body.get("city") or "").strip() or None
    if not sector:
        return None, "missing_sector"
    if not _COUNTRY_RE.fullmatch(country):
        return None, "invalid_country"
    raw_max_results = body.get("max_results")
    if raw_max_results is None or raw_max_results == "":
        max_results = config.COMPANY_LEADS_MAX_RESULTS
    else:
        try:
            max_results = int(raw_max_results)
        except (TypeError, ValueError):
            return None, "invalid_max_results"
    if max_results < 1 or max_results > config.COMPANY_LEADS_MAX_RESULTS:
        return None, "invalid_max_results"
    return {"sector": sector, "country": country, "city": city, "max_results": max_results}, None


async def _handle_create_job(request: Request, body: dict):
    payer = extract_payer_address(request)
    user_agent = request.headers.get("user-agent")
    body_excerpt = json.dumps(body)[:2000]

    query, error = _validate_query(body)
    if error is not None:
        db.log_request(
            route="company-leads", method=request.method, status="error", payer=payer,
            user_agent=user_agent, body_excerpt=body_excerpt, error_reason=error,
        )
        return JSONResponse({"error": {"reason": error}}, status_code=400)

    price = effective_price(payer, price_float(config.PRICE_COMPANY_LEADS))
    job_id = db.create_company_leads_job(query, amount_usdc=price, payer=payer)
    position = db.company_leads_queue_position(job_id)
    eta_seconds = (position + 1) * COMPANY_LEADS_POLL_SLOT_SECONDS

    db.log_request(
        route="company-leads", method=request.method, status="paid", amount_usdc=price, payer=payer,
        user_agent=user_agent, body_excerpt=body_excerpt,
    )
    return {"job_id": job_id, "eta_seconds": eta_seconds}


@router.get("/company-leads", description=ROUTE_DESCRIPTIONS["company-leads"])
async def create_company_leads_job_get(request: Request):
    params = dict(request.query_params)
    body = params if params else {"sector": "Software Development", "country": "fr", "city": "Paris"}
    return await _handle_create_job(request, body)


@router.post("/company-leads", description=ROUTE_DESCRIPTIONS["company-leads"])
async def create_company_leads_job_post(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    return await _handle_create_job(request, body)


@router.post("/company-leads/{job_id}", openapi_extra={"security": []})
async def post_company_leads_job_status(job_id: str):
    if _is_sample_job_id(job_id):
        return await get_company_leads_job_status(job_id)
    return JSONResponse(
        {"error": {"reason": "method_not_allowed", "hint": "Poll job status with GET"}},
        status_code=405,
        headers={"Allow": "GET"},
    )


@router.get("/company-leads/{job_id}", openapi_extra={"security": []})
async def get_company_leads_job_status(job_id: str):
    if _is_sample_job_id(job_id):
        return {
            "job_id": SAMPLE_JOB_ID,
            "status": "done",
            "created_at": SAMPLE_CREATED_AT,
            "finished_at": SAMPLE_FINISHED_AT,
        }

    job = db.get_company_leads_job(job_id)
    if job is None:
        return JSONResponse({"error": {"reason": "job_not_found"}}, status_code=404)

    response = {"job_id": job_id, "status": job["status"], "created_at": job["created_at"]}
    if job["status"] in ("queued", "running"):
        response["eta_seconds"] = (db.company_leads_queue_position(job_id) + 1) * COMPANY_LEADS_POLL_SLOT_SECONDS
    if job["started_at"]:
        response["started_at"] = job["started_at"]
    if job["finished_at"]:
        response["finished_at"] = job["finished_at"]
    return response


@router.post("/company-leads/{job_id}/result", openapi_extra={"security": []})
async def post_company_leads_job_result(job_id: str):
    if _is_sample_job_id(job_id):
        return await get_company_leads_job_result(job_id)
    return JSONResponse(
        {
            "error": {
                "reason": "method_not_allowed",
                "hint": "Fetch job results with GET /company-leads/{job_id}/result",
            }
        },
        status_code=405,
        headers={"Allow": "GET"},
    )


@router.get("/company-leads/{job_id}/result", openapi_extra={"security": []})
async def get_company_leads_job_result(job_id: str):
    if _is_sample_job_id(job_id):
        return _sample_result_body()

    job = db.get_company_leads_job(job_id)
    if job is None:
        return JSONResponse({"error": {"reason": "job_not_found"}}, status_code=404)

    receipt = make_receipt(None, "apify_b2b_leads", 0, job["amount_usdc"] or 0.0, steps_executed=job["leads_delivered"])

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
            "eta_seconds": (db.company_leads_queue_position(job_id) + 1) * COMPANY_LEADS_POLL_SLOT_SECONDS,
        },
        status_code=202,
    )
