#!/usr/bin/env python3
"""Reconstruit acteurs_mcp.json.z : registre MCP officiel + carnet Kairos.

Usage (depuis la racine du dépôt x402) :
  python -m scripts.build_acteurs_mcp_snapshot \\
    --out data/acteurs_mcp.json.z \\
    --db /home/kairos/etat/kairos.db
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import zlib
from datetime import date
from pathlib import Path

import httpx

REGISTRY_URL = "https://registry.modelcontextprotocol.io/v0/servers"
PAGE_SIZE = 100
MAX_PAGES = 200
EMBED_BATCH = 200
OLLAMA_EMBED_URL = "http://127.0.0.1:11435/api/embed"
EMBED_MODEL = "nomic-embed-text"
OFFICIAL_REGISTRY = "registry.modelcontextprotocol.io"


def fetch_official_servers() -> list[dict]:
    servers: list[dict] = []
    cursor: str | None = None
    with httpx.Client(timeout=30.0) as client:
        for _ in range(MAX_PAGES):
            params: dict = {"limit": PAGE_SIZE}
            if cursor:
                params["cursor"] = cursor
            resp = client.get(REGISTRY_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
            for entry in data.get("servers", []):
                server = entry.get("server", {})
                meta = entry.get("_meta", {}).get(
                    "io.modelcontextprotocol.registry/official", {}
                )
                if not meta.get("isLatest", True):
                    continue
                if meta.get("status") not in (None, "active"):
                    continue
                remotes = [
                    r.get("url") for r in server.get("remotes", []) if r.get("url")
                ]
                title = (server.get("title") or "").strip()
                name = (server.get("name") or title or "").strip()
                desc = (server.get("description") or "").strip()
                servers.append(
                    {
                        "name": name,
                        "url": remotes[0] if remotes else "",
                        "desc": desc[:400],
                        "registry": OFFICIAL_REGISTRY,
                        "updated_at": meta.get("updatedAt") or meta.get("publishedAt"),
                    }
                )
            cursor = data.get("metadata", {}).get("nextCursor")
            if not cursor:
                break
    return servers


def load_carnet(db_path: Path) -> list[dict]:
    if not db_path.is_file():
        return []
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT acteur, ou_ca, quoi, registre, embedding, dernier_vu_le
        FROM acteurs_du_dehors
        WHERE actif = 1 AND embedding IS NOT NULL
          AND TRIM(COALESCE(quoi, '')) != ''
        """
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        try:
            emb = json.loads(r["embedding"])
        except (json.JSONDecodeError, TypeError):
            continue
        if not emb:
            continue
        out.append(
            {
                "name": (r["acteur"] or "").strip(),
                "url": (r["ou_ca"] or "").strip(),
                "desc": (r["quoi"] or "")[:400],
                "registry": (r["registre"] or "").strip(),
                "updated_at": r["dernier_vu_le"],
                "emb": emb,
            }
        )
    return out


def _key(row: dict) -> str:
    url = (row.get("url") or "").strip().lower()
    if url:
        return f"url:{url}"
    return f"name:{(row.get('name') or '').strip().lower()}"


def merge(official: list[dict], carnet: list[dict]) -> list[dict]:
    """Registre officiel d'abord ; le carnet complète les entrées sans doublon URL/nom."""
    by_key: dict[str, dict] = {}
    for row in official:
        by_key[_key(row)] = row
    for row in carnet:
        k = _key(row)
        if k in by_key:
            continue
        extra = {kk: vv for kk, vv in row.items() if kk != "emb"}
        extra["_emb_preset"] = row["emb"]
        by_key[k] = extra
    return list(by_key.values())


def corpus_text(row: dict) -> str:
    name = row.get("name") or ""
    desc = row.get("desc") or ""
    return f"{name}: {desc}".strip()[:2000]


def embed_batches(texts: list[str], ollama_url: str) -> list[list[float]]:
    vectors: list[list[float]] = []
    with httpx.Client(timeout=120.0) as client:
        for i in range(0, len(texts), EMBED_BATCH):
            batch = texts[i : i + EMBED_BATCH]
            resp = client.post(
                ollama_url,
                json={"model": EMBED_MODEL, "input": batch},
            )
            resp.raise_for_status()
            data = resp.json()
            embs = data.get("embeddings")
            if not embs or len(embs) != len(batch):
                raise RuntimeError(
                    f"Ollama embed batch {i}: attendu {len(batch)}, reçu {len(embs or [])}"
                )
            vectors.extend(embs)
            print(f"  embed {min(i + EMBED_BATCH, len(texts))}/{len(texts)}", flush=True)
    return vectors


def build_rows(merged: list[dict], ollama_url: str) -> list[dict]:
    need_embed: list[tuple[int, str]] = []
    for idx, row in enumerate(merged):
        if row.get("_emb_preset") is not None:
            continue
        need_embed.append((idx, corpus_text(row)))

    if need_embed:
        texts = [t for _, t in need_embed]
        print(f"Embedding {len(texts)} entrées via Ollama…", flush=True)
        vecs = embed_batches(texts, ollama_url)
        for (idx, _), vec in zip(need_embed, vecs, strict=True):
            merged[idx]["emb"] = vec

    for row in merged:
        if row.get("_emb_preset") is not None:
            row["emb"] = row.pop("_emb_preset")
        row.pop("_emb_preset", None)

    return [
        {
            "name": r["name"],
            "url": r.get("url") or "",
            "desc": r.get("desc") or "",
            "registry": r.get("registry") or "",
            "emb": r["emb"],
            **({"updated_at": r["updated_at"]} if r.get("updated_at") else {}),
        }
        for r in merged
        if r.get("emb")
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "data" / "acteurs_mcp.json.z",
    )
    parser.add_argument("--db", type=Path, default=Path("/home/kairos/etat/kairos.db"))
    parser.add_argument("--ollama", default=OLLAMA_EMBED_URL)
    args = parser.parse_args()

    print("Téléchargement registre officiel…", flush=True)
    official = fetch_official_servers()
    print(f"  {len(official)} serveurs isLatest actifs", flush=True)

    carnet = load_carnet(args.db)
    print(f"  carnet : {len(carnet)} entrées avec embedding", flush=True)

    merged = merge(official, carnet)
    print(f"  fusion : {len(merged)} entrées uniques", flush=True)

    rows = build_rows(merged, args.ollama)
    print(f"Snapshot final : {len(rows)} lignes", flush=True)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    payload = zlib.compress(json.dumps(rows, separators=(",", ":")).encode("utf-8"), 9)
    args.out.write_bytes(payload)
    print(f"Écrit {args.out} ({len(payload)} octets compressés)", flush=True)
    print(f"snapshot_date={date.today().isoformat()}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
