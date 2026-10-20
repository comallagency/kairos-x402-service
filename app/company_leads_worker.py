"""Background worker for POST /company-leads - polls company_leads_jobs
(app/db.py), kicks off an Apify actor run per queued job, polls it to
completion, and stores the shaped result. Deliberately its own table/loop,
not a branch inside app/jobs_worker.py - see the comment on
`company_leads_jobs` in app/db.py's SCHEMA for why.

Actor: b2bsearch/b2b-leads-finder (person-level leads, each carrying their
current company's name/domain/size - the usual meaning of "lead" in B2B
sales). Chosen and its exact output fields verified with real test runs on
our own Apify account, 2026-10-20 (see docs/company-leads-dry-run-report.md)
- NOT guessed from the Store listing's own description text alone.

Data provenance (per the actor's own README, 2026-10-20): "a rolling-update
proprietary B2B database; employer-to-email matching is performed by
comparing company name and email domain" - i.e. emails are looked up from
an existing database and matched to the right current employer, never
pattern-guessed (no firstname.lastname@domain generation). The vendor does
not publish exact sourcing (likely public-profile/web aggregation, same
category as Apollo/Clearbit/ZoomInfo - standard for this market, but
opaque); this is disclosed here rather than overclaimed.

emailType is forced to "work" in build_actor_input() below, never "any": a
real 20-lead test run with "any" returned 45% personal-mailbox addresses
(gmail/yahoo/hotmail/free.fr) - not a usable professional lead and a
quality problem the buyer would be paying for. "work" filters to the
person's current-employer address only (verified 100% work-domain on a
real follow-up run), at the same per-lead price.

Processes one job fully (including polling the Apify run to completion)
before picking up the next - acceptable for a brand-new, low-volume route;
a documented limitation, not an oversight.
"""

from __future__ import annotations

import asyncio
import json
import logging

from app import config, db
from app.upstream.apify import ApifyError, get_dataset_items, get_run, start_actor_run

logger = logging.getLogger("x402.company_leads_worker")

_POLL_INTERVAL_S = 5.0
# Apify's own run-sync-get-dataset-items endpoint hard-caps at 300s; we are
# async (polling, not that endpoint) but still bound total wait per job so a
# stuck run can't hold the worker loop forever.
_RUN_DEADLINE_S = 280.0
_TERMINAL_STATES = {"SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"}


def build_actor_input(query: dict) -> dict:
    run_input: dict = {
        "countries": [query["country"]],
        "industries": [query["sector"]],
        "maxResults": query["max_results"],
        # Forced to "work", never "any": a real 20-lead test run with "any"
        # came back 45% personal-mailbox addresses (gmail/yahoo/hotmail/
        # free.fr) - not usable as a professional sales lead and a quality
        # problem the buyer would be paying for. "work" filters to the
        # person's current-employer address only, verified 100% work-domain
        # on a real follow-up run (2026-10-20) - see
        # docs/company-leads-dry-run-report.md.
        "emailType": "work",
    }
    if query.get("city"):
        run_input["cities"] = [query["city"]]
    return run_input


def _is_real_lead(item: dict) -> bool:
    """b2bsearch/b2b-leads-finder's dataset includes free, uncharged
    placeholder rows (all fields null) when a run finds fewer leads than
    maxResults - verified directly in a real test run, 2026-10-20. Never
    shape or count these as delivered leads."""
    return bool(item.get("companyName") or item.get("fullName"))


def shape_lead(item: dict) -> dict:
    return {
        "company_name": item.get("companyName"),
        "company_domain": item.get("companyDomain"),
        "company_size": item.get("companySize"),
        "company_industry": item.get("companyIndustry"),
        "contact_name": item.get("fullName"),
        "contact_title": item.get("jobTitle"),
        "email": item.get("email"),
        "email_type": item.get("emailType"),
        "linkedin_url": item.get("linkedinUrl"),
        "location": item.get("location"),
    }


async def _process_job(job: dict) -> None:
    query = json.loads(job["input_json"])
    try:
        run_id = await start_actor_run(config.APIFY_ACTOR_ID, build_actor_input(query))
    except ApifyError as exc:
        db.fail_company_leads_job(job["id"], {"reason": exc.reason})
        return

    db.mark_company_leads_job_running(job["id"], run_id)

    loop = asyncio.get_event_loop()
    deadline = loop.time() + _RUN_DEADLINE_S
    status = "RUNNING"
    run = None
    while loop.time() < deadline:
        await asyncio.sleep(_POLL_INTERVAL_S)
        try:
            run = await get_run(run_id)
        except ApifyError as exc:
            db.fail_company_leads_job(job["id"], {"reason": exc.reason, "apify_run_id": run_id})
            return
        status = run["status"]
        if status in _TERMINAL_STATES:
            break
    else:
        db.fail_company_leads_job(job["id"], {"reason": "apify_run_timeout", "apify_run_id": run_id})
        return

    if status != "SUCCEEDED":
        db.fail_company_leads_job(
            job["id"], {"reason": f"apify_run_{status.lower().replace('-', '_')}", "apify_run_id": run_id}
        )
        return

    try:
        raw_items = await get_dataset_items(run["defaultDatasetId"], config.COMPANY_LEADS_MAX_RESULTS)
    except ApifyError as exc:
        db.fail_company_leads_job(job["id"], {"reason": exc.reason, "apify_run_id": run_id})
        return

    leads = [shape_lead(item) for item in raw_items if _is_real_lead(item)]
    db.finish_company_leads_job(job["id"], {"query": query, "leads": leads, "count": len(leads)}, len(leads))


async def worker_loop(poll_interval: float = 2.0) -> None:
    logger.info("company leads worker started")
    while True:
        try:
            job = db.next_queued_company_leads_job()
            if job is None:
                await asyncio.sleep(poll_interval)
                continue
            await _process_job(job)
        except asyncio.CancelledError:
            logger.info("company leads worker stopping")
            raise
        except Exception:
            logger.exception("company leads worker loop error")
            await asyncio.sleep(poll_interval)
