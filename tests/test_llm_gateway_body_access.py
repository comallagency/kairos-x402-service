"""Guards app/handlers/llm_gateway.py::compute_ceiling_price's dependency on
x402.http.middleware.fastapi.FastAPIAdapter wrapping the real Starlette
Request as `._request`.

Why this exists: FastAPIAdapter.get_body() is a documented stub that always
returns None ("Body requires async access" - x402/http/middleware/fastapi.py),
so there is no PUBLIC way to read a POST body from inside a DynamicPrice
callback in x402==2.22.0 (pinned in pyproject.toml/Dockerfile precisely so
this doesn't move under us silently). compute_ceiling_price reaches into the
adapter's private `._request` attribute instead. If a future x402 upgrade
renames or removes that attribute, compute_ceiling_price's except-Exception
fallback would swallow the AttributeError and silently quote every request
the same generic worst-case price instead of the real per-request ceiling -
wrong prices, no crash, no log line. This test fails loudly on that
specific breakage before it ever reaches production.
"""

import asyncio

from starlette.requests import Request

from x402.http.middleware.fastapi import FastAPIAdapter
from x402.http.types import HTTPRequestContext


async def _receive():
    return {
        "type": "http.request",
        "body": b'{"model": "openai/gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]}',
        "more_body": False,
    }


def _make_request() -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/chat/completions",
        "raw_path": b"/v1/chat/completions",
        "query_string": b"",
        "headers": [],
    }
    return Request(scope, receive=_receive)


def test_fastapi_adapter_exposes_underlying_request_for_body_access() -> None:
    request = _make_request()
    adapter = FastAPIAdapter(request)

    assert hasattr(adapter, "_request"), (
        "FastAPIAdapter no longer exposes the wrapped Starlette Request as "
        "._request - app/handlers/llm_gateway.py::compute_ceiling_price can "
        "no longer read the POST body to price a request; check whether "
        "get_body() has become usable instead and switch to it."
    )
    assert adapter._request is request

    context = HTTPRequestContext(adapter=adapter, path="/v1/chat/completions", method="POST")
    body = asyncio.run(context.adapter._request.json())
    assert body["model"] == "openai/gpt-4o-mini"
    assert body["messages"][0]["content"] == "hi"
