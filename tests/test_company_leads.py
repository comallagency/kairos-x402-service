"""Tests for POST /company-leads: input validation (app/handlers/
company_leads.py), actor-input mapping and dataset shaping (app/
company_leads_worker.py). The Apify HTTP calls themselves are mocked here -
this file's signal is about OUR OWN logic, not about Apify's actor; the real
end-to-end call was verified separately against a live Apify account (see
docs/company-leads-dry-run-report.md), not re-run on every CI pass.

Fixture field names (companyName, companyDomain, companySize, email,
emailType, ...) mirror the REAL output of b2bsearch/b2b-leads-finder,
captured from a real run on 2026-10-20 - only the person/company identities
below are fictional, same convention as this repo's own JOBS_SAMPLE_OUTPUT
("Example Corp")."""

import json
from unittest.mock import AsyncMock, patch

import pytest

from app.handlers.company_leads import _validate_query
from app.company_leads_worker import _is_real_lead, _process_job, build_actor_input, shape_lead
from app.upstream.apify import ApifyError

# --- app/handlers/company_leads.py: _validate_query --------------------------


def test_validate_query_accepts_minimal_valid_input():
    query, error = _validate_query({"sector": "Software Development", "country": "FR"})
    assert error is None
    assert query == {"sector": "Software Development", "country": "fr", "city": None, "max_results": 25}


def test_validate_query_accepts_city_and_max_results():
    query, error = _validate_query(
        {"sector": "Marketing", "country": "us", "city": "Austin", "max_results": 10}
    )
    assert error is None
    assert query == {"sector": "Marketing", "country": "us", "city": "Austin", "max_results": 10}


def test_validate_query_rejects_missing_sector():
    query, error = _validate_query({"country": "fr"})
    assert query is None
    assert error == "missing_sector"


@pytest.mark.parametrize("country", ["", "france", "f", "123"])
def test_validate_query_rejects_invalid_country(country):
    query, error = _validate_query({"sector": "Software", "country": country})
    assert query is None
    assert error == "invalid_country"


@pytest.mark.parametrize("max_results", [0, -1, 26, "not-a-number"])
def test_validate_query_rejects_invalid_max_results(max_results):
    query, error = _validate_query({"sector": "Software", "country": "fr", "max_results": max_results})
    assert query is None
    assert error == "invalid_max_results"


# --- app/company_leads_worker.py: mapping and shaping -------------------------


def test_build_actor_input_maps_sector_and_country():
    run_input = build_actor_input({"sector": "Software Development", "country": "fr", "city": None, "max_results": 25})
    assert run_input == {
        "countries": ["fr"],
        "industries": ["Software Development"],
        "maxResults": 25,
        "emailType": "work",
    }


def test_build_actor_input_forces_work_email_never_any_or_personal():
    """A real 20-lead test run with emailType="any" came back 45% personal
    mailboxes (gmail/yahoo/hotmail/free.fr) - unusable as sales leads. This
    must never regress back to "any"."""
    run_input = build_actor_input({"sector": "Anything", "country": "us", "city": None, "max_results": 1})
    assert run_input["emailType"] == "work"


def test_build_actor_input_includes_city_when_given():
    run_input = build_actor_input({"sector": "Marketing", "country": "us", "city": "Austin", "max_results": 10})
    assert run_input["cities"] == ["Austin"]


def test_is_real_lead_rejects_placeholder_rows():
    # Real placeholder row shape observed in a live run: every field null.
    assert not _is_real_lead({"companyName": None, "fullName": None, "email": None})


def test_is_real_lead_accepts_a_real_row():
    assert _is_real_lead({"companyName": "Acme Corp", "fullName": "Jane Doe"})


def test_shape_lead_maps_real_field_names():
    raw = {
        "companyName": "Acme Corp",
        "companyDomain": "acme.example",
        "companySize": 42,
        "companyIndustry": "Software Development",
        "fullName": "Jane Doe",
        "jobTitle": "Head of Growth",
        "email": "jane@acme.example",
        "emailType": "work",
        "linkedinUrl": "https://www.linkedin.com/in/jane-doe-example",
        "location": "Paris, Île-de-France, France",
    }
    lead = shape_lead(raw)
    assert lead == {
        "company_name": "Acme Corp",
        "company_domain": "acme.example",
        "company_size": 42,
        "company_industry": "Software Development",
        "contact_name": "Jane Doe",
        "contact_title": "Head of Growth",
        "email": "jane@acme.example",
        "email_type": "work",
        "linkedin_url": "https://www.linkedin.com/in/jane-doe-example",
        "location": "Paris, Île-de-France, France",
    }


def test_shape_lead_preserves_emoji_in_job_title():
    """Real record captured 2026-10-20 (Diduenjoy/Alexandre Lacreuse,
    b2bsearch/b2b-leads-finder): jobTitle contained a real regional-indicator
    emoji flag sequence ("VP of Sales \U0001f1ea\U0001f1fa\U0001f1e8\U0001f1ed\U0001f1fa\U0001f1f8")
    - a person's real self-reported title, not invented or corrupted data.
    It looked "malformed" only because of how it was echoed through a
    Windows-console/WSL pipe during manual investigation, NOT because
    shape_lead() or the API response corrupts it - this pins that down at
    the actual data layer, independent of any terminal's rendering."""
    raw = {
        "companyName": "Diduenjoy",
        "companyDomain": "diduenjoy.com",
        "companySize": 34,
        "fullName": "Alexandre Lacreuse",
        "jobTitle": "VP of Sales \U0001f1ea\U0001f1fa\U0001f1e8\U0001f1ed\U0001f1fa\U0001f1f8",
        "email": "alexandre@diduenjoy.com",
        "emailType": "work",
        "location": "Paris, Île-de-France, France",
    }
    lead = shape_lead(raw)
    assert lead["contact_title"] == raw["jobTitle"]
    assert lead["location"] == "Paris, Île-de-France, France"
    # Round-trip through real JSON serialization (what actually crosses the
    # wire in the HTTP response) - confirms no corruption at the API layer.
    reparsed = json.loads(json.dumps(lead, ensure_ascii=False))
    assert reparsed["contact_title"] == raw["jobTitle"]


def test_shape_lead_handles_sparse_domain_honestly():
    """Real runs showed companyDomain absent on some rows (e.g. when the
    lead's employer is a large/well-known company) - must come back as
    None, never a guessed or invented domain."""
    raw = {"companyName": "Big Co", "companySize": 160000, "email": "x@gmail.com", "emailType": "personal"}
    lead = shape_lead(raw)
    assert lead["company_domain"] is None
    assert lead["company_name"] == "Big Co"
    assert lead["company_size"] == 160000


# --- app/company_leads_worker.py: _process_job orchestration -----------------


@pytest.mark.asyncio
async def test_process_job_happy_path(monkeypatch):
    job = {"id": "job1", "input_json": '{"sector": "Software", "country": "fr", "city": null, "max_results": 5}'}

    fake_run_succeeded = {"status": "SUCCEEDED", "defaultDatasetId": "dataset1"}
    fake_items = [
        {"companyName": "Acme Corp", "companyDomain": "acme.example", "companySize": 42, "email": "jane@acme.example", "emailType": "work", "fullName": "Jane Doe"},
        {"companyName": None, "fullName": None, "email": None},  # free placeholder row, must be dropped
    ]

    calls = {"mark_running": None, "finish": None}

    def _mark_running(job_id, run_id):
        calls["mark_running"] = (job_id, run_id)

    def _finish(job_id, result, count):
        calls["finish"] = (job_id, result, count)

    monkeypatch.setattr("app.company_leads_worker.db.mark_company_leads_job_running", _mark_running)
    monkeypatch.setattr("app.company_leads_worker.db.finish_company_leads_job", _finish)
    monkeypatch.setattr("app.company_leads_worker.asyncio.sleep", AsyncMock())

    with (
        patch("app.company_leads_worker.start_actor_run", new=AsyncMock(return_value="run1")),
        patch("app.company_leads_worker.get_run", new=AsyncMock(return_value=fake_run_succeeded)),
        patch("app.company_leads_worker.get_dataset_items", new=AsyncMock(return_value=fake_items)),
    ):
        await _process_job(job)

    assert calls["mark_running"] == ("job1", "run1")
    job_id, result, count = calls["finish"]
    assert job_id == "job1"
    assert count == 1  # the placeholder row must not be counted
    assert result["count"] == 1
    assert result["leads"][0]["company_name"] == "Acme Corp"


@pytest.mark.asyncio
async def test_process_job_marks_failed_when_apify_run_fails(monkeypatch):
    job = {"id": "job2", "input_json": '{"sector": "Software", "country": "fr", "city": null, "max_results": 5}'}
    calls = {}

    def _fail(job_id, error):
        calls["fail"] = (job_id, error)

    monkeypatch.setattr("app.company_leads_worker.db.fail_company_leads_job", _fail)
    monkeypatch.setattr("app.company_leads_worker.db.mark_company_leads_job_running", lambda *a: None)
    monkeypatch.setattr("app.company_leads_worker.asyncio.sleep", AsyncMock())

    with (
        patch("app.company_leads_worker.start_actor_run", new=AsyncMock(return_value="run1")),
        patch("app.company_leads_worker.get_run", new=AsyncMock(return_value={"status": "FAILED", "defaultDatasetId": None})),
    ):
        await _process_job(job)

    job_id, error = calls["fail"]
    assert job_id == "job2"
    assert error["reason"] == "apify_run_failed"


@pytest.mark.asyncio
async def test_process_job_marks_failed_when_actor_start_errors(monkeypatch):
    job = {"id": "job3", "input_json": '{"sector": "Software", "country": "fr", "city": null, "max_results": 5}'}
    calls = {}
    monkeypatch.setattr("app.company_leads_worker.db.fail_company_leads_job", lambda jid, err: calls.update(fail=(jid, err)))

    with patch("app.company_leads_worker.start_actor_run", new=AsyncMock(side_effect=ApifyError("apify_not_configured"))):
        await _process_job(job)

    job_id, error = calls["fail"]
    assert job_id == "job3"
    assert error["reason"] == "apify_not_configured"
