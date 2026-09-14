import asyncio
import json
import logging

from app import config, db
from app.upstream.openrouter import chat_completion_with_fallback
from app.upstream.websearch import run_web_search

logger = logging.getLogger("x402.jobs_worker")

QUERY_GEN_PROMPT = (
    "Given a subject or company name, propose up to 3 short, distinct web search "
    'queries that would help write a factual research brief about it. Respond with '
    'ONLY a JSON array of strings, e.g. ["query one", "query two"].'
)

SYNTHESIS_PROMPT = (
    "You are a research analyst. Using ONLY the search results provided (each "
    "preceded by the query that produced it), write a factual, neutral brief about "
    'the subject. Respond with ONLY a JSON object of the form '
    '{"summary": "...", "sources": ["url1", "url2"]}. If the search results are '
    "insufficient, say so plainly in the summary rather than inventing facts."
)


async def _run_job(job: dict) -> tuple[dict, int, int]:
    input_data = json.loads(job["input_json"])
    subject = input_data["subject"]
    steps = 0
    sources_seen: list[str] = []

    steps += 1
    query_data, _ = await chat_completion_with_fallback(
        [
            {"role": "system", "content": QUERY_GEN_PROMPT},
            {"role": "user", "content": subject},
        ],
        config.OPENROUTER_TRANSLATE_MODELS,
        config.OPENROUTER_LAST_RESORT_MODEL,
        max_tokens=200,
    )
    raw = query_data["choices"][0]["message"]["content"]
    try:
        queries = json.loads(raw)
        if not isinstance(queries, list):
            queries = [subject]
    except json.JSONDecodeError:
        queries = [subject]
    queries = [str(q) for q in queries][: config.OPENROUTER_MAX_SEARCHES_PER_JOB] or [subject]

    search_snippets = []
    for query in queries:
        if steps >= config.OPENROUTER_MAX_CALLS_PER_JOB - 1:
            break
        steps += 1
        try:
            results, _ = await run_web_search(query, max_results=5)
        except Exception as exc:
            logger.warning("job %s: search failed for %r: %s", job["id"], query, exc)
            continue
        for r in results:
            if r["url"] not in sources_seen:
                sources_seen.append(r["url"])
        snippet = "\n".join(f"- {r['title'] or r['url']}: {(r['extract'] or '')[:500]}" for r in results)
        search_snippets.append(f"Query: {query}\n{snippet}")

    steps += 1
    synthesis_input = "\n\n---\n\n".join(search_snippets) or "No search results were available."
    synthesis_data, _ = await chat_completion_with_fallback(
        [
            {"role": "system", "content": SYNTHESIS_PROMPT},
            {"role": "user", "content": f"Subject: {subject}\n\n{synthesis_input}"},
        ],
        config.OPENROUTER_TRANSLATE_MODELS,
        config.OPENROUTER_LAST_RESORT_MODEL,
        max_tokens=800,
    )
    raw_synth = synthesis_data["choices"][0]["message"]["content"]
    try:
        parsed = json.loads(raw_synth)
    except json.JSONDecodeError:
        parsed = {"summary": raw_synth.strip(), "sources": []}

    result_sources = parsed.get("sources") or sources_seen
    result = {"subject": subject, "summary": parsed.get("summary", ""), "sources": result_sources}
    return result, steps, len(result_sources)


async def worker_loop(poll_interval: float = 2.0):
    logger.info("jobs worker started")
    while True:
        try:
            job = db.next_queued_job()
            if job is None:
                await asyncio.sleep(poll_interval)
                continue
            db.mark_job_running(job["id"])
            try:
                result, steps, sources_read = await _run_job(job)
                db.finish_job(job["id"], result, steps, sources_read)
            except Exception as exc:
                logger.exception("job %s failed", job["id"])
                db.fail_job(job["id"], {"reason": "job_execution_error", "detail": str(exc)[:300]})
        except asyncio.CancelledError:
            logger.info("jobs worker stopping")
            raise
        except Exception:
            logger.exception("worker loop error")
            await asyncio.sleep(poll_interval)
