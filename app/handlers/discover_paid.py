import json
import zlib
from datetime import date
from functools import lru_cache
from pathlib import Path

import numpy as np
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app import db
from app.discover_freshness import combined_relevance, parse_observed_day
from app.upstream.ollama import OllamaError, embed
from app.x402_setup import KIT_TAGS, build_route_configs

router = APIRouter()

# POST /discover payant : même instantané MCP que GET /discover (discover.py),
# avec plafond de résultats plus haut (25 vs 10) et seuil min_similarity
# réglable dans le corps JSON. Le paiement x402 cible les agents qui veulent
# ces paramètres ou le flux HTTP 402 habituel du kit.

SNAPSHOT_PATH = Path("/app/data/acteurs_mcp.json.z")
MAX_RESULTS_CAP = 25
DEFAULT_MAX_RESULTS = 5
MIN_SIMILARITY = 0.30
SNAPSHOT_DATE = "2026-09-18"
SNAPSHOT_ROWS = 10101  # défaut doc ; la réponse utilise len(snapshot) en prod
_WARMUP_QUERY = "mcp server discovery"
_REFERENCE_DATE = parse_observed_day(SNAPSHOT_DATE) or date.today()


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
    combined = np.array(
        [
            combined_relevance(
                float(sub[i]),
                rows[int(idx[i])].get("updated_at"),
                _REFERENCE_DATE,
            )
            for i in range(len(sub))
        ],
        dtype=np.float32,
    )
    if len(combined) <= max_results:
        order = np.argsort(-combined)
        picked = [(float(sub[i]), rows[int(idx[i])]) for i in order]
        return match_count, picked
    top_local = np.argpartition(-combined, max_results)[:max_results]
    top_local = top_local[np.argsort(-combined[top_local])]
    picked = [(float(sub[i]), rows[int(idx[i])]) for i in top_local]
    return match_count, picked


# Nos propres routes payantes, éligibles à se promouvoir en tête de /discover
# si leur similarité dépasse le meilleur match externe. Réutilise
# build_route_configs() (déjà la source de vérité pour /.well-known/x402 et
# /discovery/resources) - pas de liste séparée à maintenir.
_own_routes_cache: dict[str, object] = {}


def _own_route_rows() -> list[dict]:
    rows = []
    for route_key, rc in build_route_configs().items():
        method, path = route_key.split(" ", 1)
        accepts = rc.accepts if isinstance(rc.accepts, list) else [rc.accepts]
        info = (rc.extensions or {}).get("bazaar", {}).get("info", {})
        input_block = info.get("input", {})
        body = input_block.get("body") if input_block.get("bodyType") == "json" else None
        rows.append({
            "name": rc.service_name or path.lstrip("/"),
            "url": rc.resource,
            "method": method,
            "description": rc.description or "",
            "price_usdc": accepts[0].price,
            "example_body": body,
        })
    # /discover ne doit jamais se promouvoir lui-meme dans ses propres
    # resultats.
    return [r for r in rows if not r["url"].endswith("/discover")]


def _embed_text_for(row: dict) -> str:
    """Seule la première phrase de la description sert a l'embedding - la
    description publiée (build_route_configs(), /.well-known/x402,
    /capabilities) reste inchangée et plus détaillée. row['name'] est
    service_name (build_route_configs()) - déjà un préfixe fonctionnel
    littéral pour les routes mesurées (voir x402_setup.py), plus besoin
    de table de substitution ici."""
    desc = row["description"] or ""
    idx = desc.find(".")
    first_sentence = desc[: idx + 1] if idx != -1 else desc
    return f"{row['name']}: {first_sentence}"


async def _load_own_routes() -> tuple[list[dict], np.ndarray]:
    """Même schéma que _load_snapshot(), mais embed() est async donc pas de
    lru_cache direct - un dict-cache calculé une seule fois par process."""
    if "matrix" in _own_routes_cache:
        return _own_routes_cache["rows"], _own_routes_cache["matrix"]
    rows = _own_route_rows()
    texts = [_embed_text_for(r) for r in rows]
    vectors = await embed(texts)
    matrix = np.asarray(vectors, dtype=np.float32)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    matrix = matrix / norms
    _own_routes_cache["rows"] = rows
    _own_routes_cache["matrix"] = matrix
    return rows, matrix


def _best_own_match(rows: list[dict], matrix: np.ndarray, query_vec: list[float]):
    q = np.asarray(query_vec, dtype=np.float32)
    qn = float(np.linalg.norm(q))
    if qn == 0 or matrix.size == 0:
        return None
    scores = matrix @ (q / qn)
    best_idx = int(np.argmax(scores))
    return float(scores[best_idx]), rows[best_idx]


async def _run_discover(q: str, max_results: int, threshold: float) -> dict:
    rows, matrix = _load_snapshot()
    vectors = await embed([q])
    query_vec = vectors[0]
    match_count, top = _top_matches(rows, matrix, query_vec, max_results, threshold)

    own_rows, own_matrix = await _load_own_routes()
    own_best = _best_own_match(own_rows, own_matrix, query_vec)
    best_external_score = top[0][0] if top else 0.0
    promote = own_best if own_best and own_best[0] >= best_external_score else None

    results = [
        {
            "name": r["name"],
            "url": r["url"] or None,
            "description": r["desc"],
            "registry": r["registry"] or None,
            "relevance": round(score, 4),
            **(
                {"observed_at": r["updated_at"]}
                if r.get("updated_at")
                else {}
            ),
        }
        for score, r in top
    ]
    if promote:
        score, r = promote
        results = [{
            "name": r["name"],
            "url": r["url"],
            "method": r["method"],
            "description": r["description"],
            "price_usdc": r["price_usdc"],
            "example_body": r["example_body"],
            "relevance": round(score, 4),
            "source": "agentindex-x402",
        }] + results[: max(0, max_results - 1)]

    return {
        "q": q,
        "snapshot_date": SNAPSHOT_DATE,
        "snapshot_rows": len(rows),
        "min_similarity": threshold,
        "matches": match_count,
        "results": results,
    }


async def warm_discover_cache() -> None:
    """Précharge le snapshot et réveille Ollama pour que le premier client payant
    ne paie pas le coût du cold start."""
    # Discovery data is an optional runtime volume. Its absence must not take
    # every unrelated paid route offline during a deploy.
    try:
        _load_snapshot()
    except (OSError, ValueError, zlib.error, json.JSONDecodeError):
        return
    try:
        await embed([_WARMUP_QUERY])
        await _load_own_routes()
    except OllamaError:
        pass


def _body_error(reason: str, detail: str = "") -> JSONResponse:
    payload: dict = {"error": {"reason": reason}}
    if detail:
        payload["error"]["detail"] = detail[:200]
    return JSONResponse(payload, status_code=400)


_SAMPLE_QUERY = "extract text from a PDF"


@router.get("/discover/sample", openapi_extra={"security": []}, tags=KIT_TAGS + ["discovery"])
async def discover_sample():
    """Exemple statique de forme — la route utile est POST /discover (payant)."""
    try:
        result = await _run_discover(_SAMPLE_QUERY, DEFAULT_MAX_RESULTS, MIN_SIMILARITY)
    except OllamaError as exc:
        return JSONResponse(
            {"error": {"reason": "upstream_error", "detail": str(exc)[:200]}},
            status_code=502,
        )
    return {
        **result,
        "note": "Paid route: POST /discover (USDC on Base). This sample is free.",
    }


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
