"""Normalise Accept pour les sondes MCP qui n'envoient que application/json.

FastMCP streamable HTTP exige « application/json, text/event-stream ». Arclan et
d'autres registres handshake parfois sans event-stream ; on complète le header
sans retirer ce que le client a demandé.
"""


class McpAcceptCompatMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path") or ""
        if not path.startswith("/mcp"):
            await self.app(scope, receive, send)
            return

        headers = list(scope.get("headers") or [])
        accept_idx = None
        accept_val = None
        for i, (name, value) in enumerate(headers):
            if name.lower() == b"accept":
                accept_idx = i
                accept_val = value.decode("latin-1")
                break

        if accept_val is None:
            headers.append((b"accept", b"application/json, text/event-stream"))
        elif "text/event-stream" not in accept_val:
            patched = accept_val.strip()
            if patched and not patched.endswith(","):
                patched = f"{patched}, text/event-stream"
            else:
                patched = f"{patched} text/event-stream"
            if accept_idx is None:
                headers.append((b"accept", patched.encode("latin-1")))
            else:
                headers[accept_idx] = (headers[accept_idx][0], patched.encode("latin-1"))

        new_scope = dict(scope)
        new_scope["headers"] = headers
        await self.app(new_scope, receive, send)
