#!/usr/bin/env python3
"""Sonde catalogue (2026-10-01) - toutes les 15 min, contre le domaine
public reel (pas le port interne), exactement comme le ferait une sonde de
reputation externe (AgentEconomyReport et les 4 autres, voir
scripts/ingest_daily_nginx_stats.py::PROBER_PATTERNS) : un GET non paye sur
chaque ressource actuellement listee (attendu: 402) et sur chaque route
retiree (attendu: 410, confirme qu'elle n'est jamais repassee a 200 par
regression). Ecrit dans route_checks (meme table que controleur.py,
check_name='probe_402') - pas une nouvelle table parallele.

Cron (containerise, meme motif que scripts/ingest_daily_nginx_stats.py) :
    */15 * * * * cd /opt/x402/app && docker compose run --rm x402_blue python -m scripts.probe_catalog_health >> logs/catalog_probe.log 2>&1

Alerte : une ligne ERREUR explicite sur stderr (visible dans
logs/catalog_probe.log) pour chaque ressource qui ne repond pas 402/410 -
aucun canal push (email/Telegram/Slack) n'existe encore dans ce projet,
voir le rapport du 2026-10-01 pour la decision a prendre a ce sujet.
"""
import sys
from pathlib import Path
from urllib.parse import urlparse

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import db
from app.retired_routes import RETIRED_ROUTES

PUBLIC_URL = "https://x402.agentindex.world"


def _listed_paths(client: httpx.Client) -> list[str]:
    resp = client.get(f"{PUBLIC_URL}/.well-known/x402")
    resp.raise_for_status()
    data = resp.json()
    paths = set()
    for resource in data.get("resources", []):
        path = urlparse(resource["resource"]).path
        if path and path != "/":
            paths.add(path)
    return sorted(paths)


def main() -> int:
    failures = 0
    checked = 0

    with httpx.Client(timeout=15.0) as client:
        try:
            listed = _listed_paths(client)
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            print(f"ERREUR: lecture du catalogue public impossible: {exc}", file=sys.stderr)
            db.record_route_check("_catalog", "probe_402", False, str(exc)[:500])
            return 1

        targets = [(p, 402) for p in listed] + [(p, 410) for p in RETIRED_ROUTES]

        for path, expected in targets:
            checked += 1
            try:
                resp = client.get(f"{PUBLIC_URL}{path}")
                status = resp.status_code
            except httpx.HTTPError as exc:
                status = None
                detail = f"request error: {exc}"
            else:
                detail = f"status={status} expected={expected}"

            passed = status == expected
            if not passed:
                failures += 1
                print(f"ERREUR: {path} -> {detail} (sonde non payee, GET)", file=sys.stderr)

            db.record_route_check(path, "probe_402", passed, detail)

    print(f"{checked} ressource(s) sondee(s), {failures} echec(s).")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
