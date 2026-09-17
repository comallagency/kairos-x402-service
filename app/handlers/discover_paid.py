import json
import zlib
from functools import lru_cache
from pathlib import Path

import numpy as np
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app import db
from app.upstream.ollama import OllamaError, embed
from app.x402_setup import KIT_TAGS

router = APIRouter()

# Un POST /discover PAYANT, distinct du GET /discover gratuit (app/handlers/
# discover.py). Le GET passe par SearXNG — une recherche web qui reclasse ce
# qu'elle a indexe. Celui-ci interroge un instantané de la base locale de
# Kairos : 2190 serveurs MCP actifs, observés dehors, chacun avec un embedding
# nomic-embed-text (768 dims) précalculé. Le classement est un produit scalaire
# sur des vecteurs déjà normalisés : un appel d'embedding pour la requête,
# aucun pour les résultats.
#
# Pourquoi payer : le GET gratuit dégrade en classement SearXNG brut quand
# l'embedding échoue. Celui-ci garantit le classement sémantique — vecteurs
# précalculés, fraîcheur datée, seuil de similarité explicite.

SNAPSHOT_PATH = Path("/app/data/acteurs_mcp.json.z")
MAX_RESULTS_CAP = 25
DEFAULT_MAX_RESULTS = 5
MIN_SIMILARITY = 0.30
SNAPSHOT_DATE = "2026-09-17"
SNAPSHOT_ROWS = 2947
_WARMUP_QUERY = "mcp server discovery"


@lru_cache(maxsize=1)
def _load_snapshot() -> tuple[list[dict], np.ndarray]:
    data = zlib.decompress(SNAPSHOT_PATH.read_bytes())
    rows: list[dict] = json.loads(data)
    matrix = np.asarray([r["emb"] for r in rows], dtype=np.float32)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return rows, matrix / norms


def _top_matches(
    rows: list[dict], matrix: np.ndarray, query_vec: list[float], max_results: int, threshold: float
) -> tuple[int, list[tuple[float, dict]]]:
    q = np.asarray(query_vec, dtype=np.float32)
    qn = float(np.linalg.norm(q))
    if qn == 0:
        return 0, []
    q = q / qn
    scores = matrix @ q
    above = scores >= threshold
    match_count = int(above.sum())
    if match_count == 0:
        return 0, []
    idx = np.where(above)[0]
    sub = scores[idx]
    if len(sub) <= max_results:
        order = np.argsort(-sub)
        picked = [(float(sub[i]), rows[int(idx[i])]) for i in order]
        return match_count, picked
    top_local = np.argpartition(-sub, max_results)[:max_results]
    top_local = top_local[np.argsort(-sub[top_local])]
    picked = [(float(sub[i]), rows[int(idx[i])]) for i in top_local]
    return match_count, picked


async def _run_discover(q: str, max_results: int, threshold: float) -> dict:
    rows, matrix = _load_snapshot()
    vectors = await embed([q])
    match_count, top = _top_matches(rows, matrix, vectors[0], max_results, threshold)
    return {
        "q": q,
        "snapshot_date": SNAPSHOT_DATE,
        "snapshot_rows": SNAPSHOT_ROWS,
        "min_similarity": threshold,
        "matches": match_count,
        "results": [
            {
                "name": r["name"],
                "url": r["url"] or None,
                "description": r["desc"],
                "registry": r["registry"] or None,
                "relevance": round(score, 4),
            }
            for score, r in top
        ],
    }


async def warm_discover_cache() -> None:
    """Précharge le snapshot et réveille Ollama pour que le premier client payant
    ne paie pas le coût du cold start."""
    _load_snapshot()
    try:
        await embed([_WARMUP_QUERY])
    except OllamaError:
        pass


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
        alt = body.get("query")
        if isinstance(alt, str) and alt.strip():
            q = alt
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
