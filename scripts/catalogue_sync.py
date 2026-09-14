#!/usr/bin/env python3
"""Snapshot mécanique du catalogue Bazaar CDP (public, sans clé) - zéro appel
de modèle. Tourne une fois par nuit (conteneurisé, comme chain_payments.py)
pour que Prospecteur (côté PC, voir usine/prospecteur.md) n'ait pas à
retélécharger ~16 500 ressources à chaque session :

    0 1 * * * cd /opt/x402/app && docker compose run --rm x402 python -m scripts.catalogue_sync >> logs/catalogue_sync.log 2>&1

Écrit un fichier JSON compact (pas la base) - Prospecteur le lit directement
en SSH ou via /admin/usine.json (voir app/admin.py). Aucune interprétation
ici : juste une réduction de volume (les champs utiles, pas le blob
`extensions` complet par ressource) et un décompte brut par mot-clé de
description, pour donner à Prospecteur un point de départ, pas une analyse.
"""
import asyncio
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import httpx

DISCOVERY_URL = "https://api.cdp.coinbase.com/platform/v2/x402/discovery/resources"
PAGE_SIZE = 100
SNAPSHOT_PATH = Path(__file__).resolve().parent.parent / "data" / "catalogue_snapshot.json"

# Rough, mechanical keyword buckets over resource descriptions - not a real
# categorization (that's Prospecteur's job), just enough structure to avoid
# re-reading 16 500 raw descriptions every session.
KEYWORD_BUCKETS = [
    "search", "translate", "ocr", "pdf", "image", "audio", "video", "scrape",
    "email", "sms", "weather", "crypto", "balance", "price", "news", "social",
    "code", "summarize", "extract", "convert", "geocode", "map",
]


async def fetch_all() -> list[dict]:
    items: list[dict] = []
    offset = 0
    async with httpx.AsyncClient(timeout=30.0) as client:
        while True:
            resp = await client.get(DISCOVERY_URL, params={"limit": PAGE_SIZE, "offset": offset})
            resp.raise_for_status()
            data = resp.json()
            page_items = data.get("items", [])
            items.extend(page_items)
            total = data.get("pagination", {}).get("total", 0)
            offset += PAGE_SIZE
            if offset >= total or not page_items:
                break
    return items


def compact(item: dict) -> dict:
    accepts = item.get("accepts") or [{}]
    first = accepts[0]
    return {
        "resource": item.get("resource"),
        "description": (item.get("description") or "")[:300],
        "network": first.get("network"),
        "amount": first.get("amount"),
        "payTo": first.get("payTo"),
        "quality": item.get("quality"),
        "lastUpdated": item.get("lastUpdated"),
    }


def bucket_counts(items: list[dict]) -> dict[str, int]:
    counts = Counter()
    for item in items:
        text = (item.get("description") or "").lower()
        for keyword in KEYWORD_BUCKETS:
            if re.search(rf"\b{keyword}\w*", text):
                counts[keyword] += 1
    return dict(counts)


async def main() -> None:
    items = await fetch_all()
    compacted = [compact(i) for i in items]
    snapshot = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_resources": len(compacted),
        "keyword_counts": bucket_counts(items),
        "resources": compacted,
    }
    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_PATH.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    print(f"{len(compacted)} ressources écrites dans {SNAPSHOT_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
