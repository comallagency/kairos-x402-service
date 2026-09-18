import asyncio

from app.handlers.jobs import (
    SAMPLE_JOB_ID,
    get_job_result,
    get_job_status,
    jobs_sample,
)


def test_jobs_sample_and_sample_id_poll_match() -> None:
    sample = asyncio.run(jobs_sample())
    status = asyncio.run(get_job_status(SAMPLE_JOB_ID))
    result = asyncio.run(get_job_result(SAMPLE_JOB_ID))

    assert status["job_id"] == SAMPLE_JOB_ID
    assert status["status"] == "done"
    assert result == sample
