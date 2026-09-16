import json
import math
import zlib
from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app import config, db
from app.upstream.ollama import OllamaError, embed
from app.x402_setup import KIT_TAGS

router = APIRouter()

# Un POST /discover PAYANT, distinct du GET /discover gratuit (app/handlers/
# discover.py). Le GET passe par SearXNG — une recherche web qui reclasse ce
# qu'elle a indexe. Celui-ci interroge un instantané de la base locale de
# Kairos : 2190 serveurs MCP actifs, observés dehors, chacun avec un embedding
# nomic-embed-text (768 dims) précalculé. Le classement est un simple produit
# scalaire sur des vecteurs déjà là : un appel d'embedding pour la requête,
# aucun pour les résultats.
#
# Pourquoi payer : le GET gratuit dégrade en classement SearXNG brut quand
# l'embedding échoue. Celui-ci garantit le classement sémantique — vecteurs
# précalculés, fraîcheur datée, seuil de similarité explicite. La différence
# de service est réelle et mesurable ; le prix aussi.

SNAPSHOT_PATH = Path("/app/data/acteurs_mcp.json.z")
MAX_RESULTS_CAP = 25
DEFAULT_MAX_RESULTS = 5
MIN_SIMILARITY = 0.30
#: Date du snapshot, réécrite à chaque export depuis la base de Kairos.
SNAPSHOT_DATE = "2026-09-16"
SNAPSHOT_ROWS = 2190


@lru_cache(maxsize=1)
def _load_snapshot() -> list[dict]:
    data = zlib.decompress(SNAPSHOT_PATH.read_bytes())
    return json.loads(data)


def _cosine(a: list[float], b: list[float]) -> float:
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0 or nb == 0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


def _shape(rows: list[dict], threshold: float) -> list[dict]:
    return [
        {
            "name": r["name"],
            "url": r["url"] or None,
            "description": r["desc"],
            "registry": r["registry"] or None,
            "relevance": r["relevance"],
        }
        for r in rows
        if r["relevance"] >= threshold
    ]


async def _run_discover(q: str, max_results: int, threshold: float) -> dict:
    snapshot = _load_snapshot()
    vectors = await embed([q])
    query_vec = vectors[0]
    scored = []
    for r in snapshot:
        score = _cosine(query_vec, r["emb"])
        if score >= threshold:
            scored.append((score, r))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    top = scored[:max_results]
    return {
        "q": q,
        "snapshot_date": SNAPSHOT_DATE,
        "snapshot_rows": SNAPSHOT_ROWS,
        "min_similarity": threshold,
        "matches": len(scored),
        "results": [
            {"name": r["name"], "url": r["url"] or None, "description": r["desc"],
             "registry": r["registry"] or None, "relevance": round(score, 4)}
            for score, r in top
        ],
    }


def _body_error(reason: str, detail: str = "") -> JSONResponse:
    payload: dict = {"error": {"reason": reason}}
    if detail:
        payload["error"]["detail"] = detail[:200]
    return JSONResponse(payload, status_code=400)


@router.post("/discover", tags=KIT_TAGS + ["discovery", "mcp", "semantic"])
async def discover_paid(request: Request):
    user_agent = request.headers.get("user-agent")
    try:
        body = await request.json()
    except Exception:
        db.log_request(
            route="discover", method="POST", status="error",
            user_agent=user_agent, error_reason="invalid_json",
        )
        return _body_error("invalid_json", "body must be a JSON object")

    if not isinstance(body, dict):
        db.log_request(
            route="discover", method="POST", status="error",
            user_agent=user_agent, error_reason="invalid_body",
        )
        return _body_error("invalid_body", "body must be a JSON object")

    q = body.get("q")
    if not isinstance(q, str) or not q.strip():
        db.log_request(
            route="discover", method="POST", status="error",
            user_agent=user_agent, error_reason="missing_q",
        )
        return _body_error("missing_q", "q is required and must be a non-empty string")

    max_results = body.get("max_results", DEFAULT_MAX_RESULTS)
    if not isinstance(max_results, int) or isinstance(max_results, bool) or not 1 <= max_results <= MAX_RESULTS_CAP:
        max_results = DEFAULT_MAX_RESULTS
    threshold = body.get("min_similarity", MIN_SIMILARITY)
    if not isinstance(threshold, int | float) or isinstance(threshold, bool) or not 0.0 <= threshold < 1.0:
        threshold = MIN_SIMILARITY

    try:
        result = await _run_discover(q.strip()[:500], max_results, float(threshold))
    except OllamaError as exc:
        db.log_request(
            route="discover", method="POST", status="error",
            user_agent=user_agent, error_reason=str(exc)[:200],
        )
        return JSONResponse(
            {"error": {"reason": "upstream_error", "detail": str(exc)[:200]}}, status_code=502
        )

    db.log_request(
        route="discover", method="POST", status="paid",
        user_agent=user_agent, body_excerpt=q[:2048],
    )
    return result
