"""One-off dry-run script (not a test, not committed to CI) - boots the real
FastAPI app in-process, confirms route/OpenAPI/well-known/MCP wiring, then
creates a REAL company-leads job and lets the real background worker run it
against the REAL Apify actor (small real cost, ~1-3 leads) end to end. Run
with: uv run python scripts/dry_run_company_leads.py
Requires APIFY_API_TOKEN set in the environment/.env.
"""

import asyncio
import json
import time

from fastapi.testclient import TestClient

from app import config, db
from app.main import app


def main() -> None:
    assert config.APIFY_API_TOKEN, "APIFY_API_TOKEN not set - cannot do a real dry run"

    with TestClient(app) as client:
        print("--- GET /company-leads/sample (free) ---")
        r = client.get("/company-leads/sample")
        print(r.status_code, sorted(r.json().keys()))

        print("\n--- GET /openapi.json: company-leads entry ---")
        spec = client.get("/openapi.json").json()
        post_op = spec["paths"]["/company-leads"]["post"]
        print("x-payment-info present:", "x-payment-info" in post_op)

        print("\n--- GET /.well-known/x402: company-leads pricing ---")
        resources = client.get("/.well-known/x402").json()["resources"]
        for m in resources:
            if m["resource"].endswith("/company-leads"):
                print(m["method"], m["resource"], "->", [a["price"] for a in m["accepts"]])

        print("\n--- Creating a REAL company-leads job (small, cheap query) ---")
        job_id = db.create_company_leads_job(
            {"sector": "Software Development", "country": "fr", "city": "Paris", "max_results": 3},
            amount_usdc=0.30,
            payer="0xDRYRUN0000000000000000000000000000000000",
        )
        print("job_id:", job_id)

    async def _run_one_job():
        from app.company_leads_worker import _process_job

        job = db.get_company_leads_job(job_id)
        t0 = time.monotonic()
        await _process_job(job)
        print(f"worker finished in {time.monotonic() - t0:.1f}s")

    asyncio.run(_run_one_job())

    job = db.get_company_leads_job(job_id)
    print("\n--- Final job state ---")
    print("status:", job["status"])
    print("leads_delivered:", job["leads_delivered"])
    if job["status"] == "done":
        result = json.loads(job["result_json"])
        print("count:", result["count"])
        for lead in result["leads"]:
            print(" -", lead["company_name"], "|", lead["company_domain"], "|", lead["company_size"], "|", lead["email"])
    else:
        print("error:", job["error_json"])


if __name__ == "__main__":
    main()
