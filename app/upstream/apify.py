"""Minimal Apify REST client for POST /company-leads
(app/company_leads_worker.py) - starts an actor run, polls its status, and
fetches the resulting dataset. No SDK dependency: 3 endpoints, plain httpx,
same pattern as the other thin upstream wrappers in this package.
"""

from __future__ import annotations

import httpx

from app import config

_BASE = "https://api.apify.com/v2"
_START_TIMEOUT_S = 30.0
_STATUS_TIMEOUT_S = 15.0
_DATASET_TIMEOUT_S = 30.0


class ApifyError(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _token() -> str:
    if not config.APIFY_API_TOKEN:
        raise ApifyError("apify_not_configured")
    return config.APIFY_API_TOKEN


async def start_actor_run(actor_id: str, run_input: dict) -> str:
    """Starts an actor run asynchronously (does not wait for it to finish) and
    returns the run id."""
    token = _token()
    async with httpx.AsyncClient(timeout=_START_TIMEOUT_S) as client:
        response = await client.post(
            f"{_BASE}/acts/{actor_id}/runs",
            params={"token": token},
            json=run_input,
        )
    if response.status_code >= 400:
        raise ApifyError(f"apify_start_failed_{response.status_code}")
    return response.json()["data"]["id"]


async def get_run(run_id: str) -> dict:
    """Raw run object - callers read `status` ('READY'/'RUNNING'/'SUCCEEDED'/
    'FAILED'/'ABORTED'/'TIMED-OUT') and `defaultDatasetId` once terminal."""
    token = _token()
    async with httpx.AsyncClient(timeout=_STATUS_TIMEOUT_S) as client:
        response = await client.get(f"{_BASE}/actor-runs/{run_id}", params={"token": token})
    if response.status_code >= 400:
        raise ApifyError(f"apify_status_failed_{response.status_code}")
    return response.json()["data"]


async def get_dataset_items(dataset_id: str, limit: int) -> list[dict]:
    token = _token()
    async with httpx.AsyncClient(timeout=_DATASET_TIMEOUT_S) as client:
        response = await client.get(
            f"{_BASE}/datasets/{dataset_id}/items",
            params={"token": token, "limit": limit, "format": "json"},
        )
    if response.status_code >= 400:
        raise ApifyError(f"apify_dataset_failed_{response.status_code}")
    return response.json()
