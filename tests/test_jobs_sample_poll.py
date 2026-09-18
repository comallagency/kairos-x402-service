import asyncio

from app.handlers.jobs import (
    OPENAPI_JOB_ID_PLACEHOLDER,
    SAMPLE_JOB_ID,
    get_job_result,
    get_job_status,
    jobs_sample,
    post_job_result,
    post_job_status,
)


def test_jobs_sample_and_sample_id_poll_match() -> None:
    sample = asyncio.run(jobs_sample())
    status = asyncio.run(get_job_status(SAMPLE_JOB_ID))
    result = asyncio.run(get_job_result(SAMPLE_JOB_ID))

    assert status["job_id"] == SAMPLE_JOB_ID
    assert status["status"] == "done"
    assert result == sample


def test_openapi_job_id_placeholder_returns_sample_shape() -> None:
    status = asyncio.run(get_job_status(OPENAPI_JOB_ID_PLACEHOLDER))
    result = asyncio.run(get_job_result(OPENAPI_JOB_ID_PLACEHOLDER))
    sample = asyncio.run(jobs_sample())

    assert status["status"] == "done"
    assert result == sample


def test_openapi_placeholder_post_matches_get() -> None:
    status = asyncio.run(post_job_status(OPENAPI_JOB_ID_PLACEHOLDER))
    result = asyncio.run(post_job_result(OPENAPI_JOB_ID_PLACEHOLDER))
    sample = asyncio.run(jobs_sample())

    assert status["status"] == "done"
    assert result == sample


def test_jobs_sample_result_path_matches_sample() -> None:
    from app.handlers.jobs import jobs_sample_result

    sample = asyncio.run(jobs_sample())
    via_result = asyncio.run(jobs_sample_result())
    assert via_result == sample
