#!/usr/bin/env python3
"""Ajoute updated_at au snapshot MCP existant sans recalculer les embeddings."""

from __future__ import annotations

import argparse
import json
import sys
import zlib
from pathlib import Path

from scripts.build_acteurs_mcp_snapshot import (
    _key,
    fetch_official_servers,
    load_carnet,
)


def enrich(rows: list[dict], official: list[dict], carnet: list[dict]) -> tuple[int, list[dict]]:
    dates: dict[str, str] = {}
    for row in official + carnet:
        ts = row.get("updated_at")
        if not ts:
            continue
        dates[_key(row)] = str(ts)
    patched = 0
    out: list[dict] = []
    for row in rows:
        copy = dict(row)
        if not copy.get("updated_at"):
            ts = dates.get(_key(copy))
            if ts:
                copy["updated_at"] = ts
                patched += 1
        out.append(copy)
    return patched, out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--path",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "data" / "acteurs_mcp.json.z",
    )
    parser.add_argument("--db", type=Path, default=Path("/home/kairos/etat/kairos.db"))
    args = parser.parse_args()

    raw = zlib.decompress(args.path.read_bytes())
    rows: list[dict] = json.loads(raw)
    print(f"Lecture {len(rows)} lignes depuis {args.path}", flush=True)

    print("Registre officiel…", flush=True)
    official = fetch_official_servers()
    carnet = load_carnet(args.db) if args.db.is_file() else []
    patched, out = enrich(rows, official, carnet)
    with_dates = sum(1 for r in out if r.get("updated_at"))
    print(f"  {patched} lignes enrichies ; {with_dates}/{len(out)} portent updated_at", flush=True)

    payload = zlib.compress(json.dumps(out, separators=(",", ":")).encode("utf-8"), 9)
    args.path.write_bytes(payload)
    print(f"Écrit {args.path} ({len(payload)} octets)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
