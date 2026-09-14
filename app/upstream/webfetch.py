"""Shared "fetch a specific URL and pull clean content out of it" logic for
/web-read, /extract and /summarize - distinct from app/upstream/websearch.py,
which searches (a query) rather than reads (a known URL). Zero upstream cost:
a direct HTTP fetch of the page itself plus local, deterministic extraction
(trafilatura) - no search API call, no model call, no new key.
"""

import httpx
import trafilatura

FETCH_TIMEOUT_SECONDS = 20.0
MAX_CONTENT_BYTES = 10 * 1024 * 1024  # 10MB - generous for an article/PDF page, caps abuse
USER_AGENT = "AgentIndexBot/1.0 (+https://agentindex.world; read-only content fetch)"


class FetchError(Exception):
    pass


def _validate_url(url: str) -> None:
    if not url or not isinstance(url, str) or not (url.startswith("http://") or url.startswith("https://")):
        raise FetchError("invalid_url")


async def fetch_bytes(url: str) -> tuple[bytes, str]:
    """Fetch raw bytes from a URL - used by /pdf for a PDF-by-URL input.
    Returns (body, content_type). Never returns a partial body silently
    truncated at MAX_CONTENT_BYTES - that case is a hard error instead."""
    _validate_url(url)
    try:
        async with httpx.AsyncClient(timeout=FETCH_TIMEOUT_SECONDS, follow_redirects=True) as client:
            async with client.stream("GET", url, headers={"User-Agent": USER_AGENT}) as resp:
                if resp.status_code >= 400:
                    raise FetchError(f"upstream_status_{resp.status_code}")
                content_type = resp.headers.get("content-type", "")
                chunks = []
                total = 0
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    if total > MAX_CONTENT_BYTES:
                        raise FetchError("content_too_large")
                    chunks.append(chunk)
                return b"".join(chunks), content_type
    except httpx.TimeoutException:
        raise FetchError("fetch_timeout")
    except httpx.RequestError as exc:
        raise FetchError(f"fetch_failed: {exc}"[:200])


async def fetch_html(url: str) -> str:
    """Fetch a URL and decode it as text - used by /web-read, /extract, /summarize."""
    body, _content_type = await fetch_bytes(url)
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        return body.decode("utf-8", errors="replace")


def extract_markdown(html: str, url: str | None = None) -> str | None:
    """Cleaned main-content markdown - navigation, ads and boilerplate
    stripped, links resolved (trafilatura resolves relative links against
    `url` itself). None if no extractable article content was found - the
    caller must treat that as an explicit error, never an empty success."""
    return trafilatura.extract(html, url=url, output_format="markdown")


def extract_title(html: str, url: str | None = None) -> str | None:
    metadata = trafilatura.extract_metadata(html, default_url=url)
    return metadata.title if metadata else None
