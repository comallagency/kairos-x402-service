"""Normalise les requetes MCP qui n'arrivent pas dans la forme que FastMCP attend.

Deux cas, mesures en logs nginx :

1. Accept manquant ou incomplet. FastMCP streamable HTTP exige
   « application/json, text/event-stream ». Arclan et d'autres registres
   handshake parfois sans event-stream ; on complete le header sans retirer
   ce que le client a demande.

2. Chemin sans slash final sur un point de montage MCP. Starlette redirige
   /mcp -> /mcp/ (307) via redirect_slashes, et les deux montages plus longs
   (/mcp/relationship-memory, /mcp/coordination-thread) ont le meme probleme
   avec un filet de rattrapage qui rate ce cas precis (voir main.py). Un
   307 est invisible pour tout client qui ne suit pas les redirections sur
   POST - releve a 57k+ POST /mcp et 42 POST /mcp/relationship-memory dans
   les journaux du 2026-10-20, avant ce correctif. On reecrit le chemin en
   interne (pas de redirection HTTP) pour que ces clients recoivent la
   vraie reponse du premier coup.
"""

_MCP_TRAILING_SLASH_PATHS = frozenset(
    {
        "/mcp",
        "/mcp/relationship-memory",
        "/mcp/coordination-thread",
    }
)


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

        if path in _MCP_TRAILING_SLASH_PATHS:
            new_scope["path"] = path + "/"
            raw_path = new_scope.get("raw_path")
            if raw_path:
                new_scope["raw_path"] = raw_path + b"/"

        await self.app(new_scope, receive, send)
