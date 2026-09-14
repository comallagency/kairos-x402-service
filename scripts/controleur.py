#!/usr/bin/env python3
"""Contrôleur - mécanique, zéro appel de modèle. Teste TOUTES les routes
(3 coeur + tout app/generated/routes_registry.yaml) contre les mêmes
critères que les catalogues externes utilisent réellement pour indexer ou
rejeter - jamais une supposition, toujours l'outil réel :

- npx -y @agentcash/discovery@latest discover <origine> -> 0 avertissement
- npx -y @agentcash/discovery@latest check <url> -> prix/protocole lus
- CDP POST /platform/v2/x402/validate -> valid + simulation.outcome=accepted
- Sonde 5 méthodes sans corps (GET/HEAD/PUT/PATCH/DELETE) -> jamais de 5xx
  (c'est exactement ce que fait le crawler x402scan, voir BRIEF-CORRECTIONS.md
  - c'est ce test qui a détecté le 502 du 05/09)
- /sample renvoie un vrai résultat (200, JSON, non vide)
- Taille de l'en-tête payment-required mesurée (alerte avant les 32 Ko de
  buffer nginx)

Cron nocturne (conteneurisé, exactement comme chain_payments.py) :
    0 2 * * * cd /opt/x402/app && docker compose run --rm x402 python -m scripts.controleur

Usage à la demande, un seul verrou de publication avant que Crieur
n'enregistre une route (voir usine/crieur.md) :
    docker compose run --rm x402 python -m scripts.controleur --route <slug>

Invoqué via `-m` (pas `python scripts/controleur.py`) : `scripts/` est un
sous-dossier, pas la racine où vit chain_payments.py - lancé comme fichier,
son propre dossier (pas /app) atterrirait sur sys.path et `import app`
échouerait. `-m` place le cwd (/app, le WORKDIR du conteneur) sur sys.path,
comme chain_payments.py en profite implicitement à la racine.

Une route qui échoue N'EST JAMAIS marquée prête à publier - le script ne
modifie jamais le registre lui-même, il se contente de journaliser le
résultat. C'est Crieur (côté PC) qui lit ce résultat avant d'enregistrer.
"""
import argparse
import asyncio
import json
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from app import config, db
from app.x402_setup import build_route_configs

PROBE_METHODS = ["GET", "HEAD", "PUT", "PATCH", "DELETE"]
HEADER_SIZE_WARN_BYTES = 16 * 1024  # headroom under the 32k nginx buffer

# Registering (x402scan/mppscan, see agentcash register) is NOT the same as
# being searchable - agentcash.dev's own search index is a third, separate
# system that `agentcash register` never touches (confirmed by reading the
# actual published CLI source, 2026-09-06: register posts only to
# x402scan.com and mppscan.com; `agentcash search` queries agentcash.dev/api/
# search, a domain register never calls). Nothing else in this project
# checked real-world discoverability before this - register succeeding was
# being mistaken for being found. See BRIEF-CORRECTIONS.md for the full
# writeup and the OpenAPI gaps found by diffing against an indexed
# competitor (missing explicit `security: []`, missing top-level
# `x-agentcash-guidance`).
INDEX_CHECK_INTERVAL_HOURS = 20  # ~daily regardless of how often controleur.py itself runs
OWN_DOMAIN = "x402.agentindex.world"
KIT_KEYWORDS = [
    "read a web page and return clean markdown",
    "extract structured data from a webpage",
    "fact check a claim",
]
CATALOGUE_SNAPSHOT_PATH = Path(__file__).resolve().parent.parent / "data" / "catalogue_snapshot.json"


def _npx(*args: str, timeout: int = 60) -> tuple[int, str]:
    try:
        result = subprocess.run(
            ["npx", "-y", *args], capture_output=True, text=True, timeout=timeout
        )
        return result.returncode, result.stdout + result.stderr
    except Exception as exc:
        return 1, str(exc)


def check_discover(origin: str) -> tuple[bool, str]:
    code, output = _npx("@agentcash/discovery@latest", "discover", origin, timeout=90)
    if code != 0:
        return False, f"discover a échoué (code {code}): {output[:300]}"
    warning_count = output.count("[warn]") + output.count("[info]")
    if warning_count > 0:
        return False, f"{warning_count} avertissement(s): {output[:500]}"
    return True, "0 avertissement"


def check_route_discover(url: str) -> tuple[bool, str]:
    code, output = _npx("@agentcash/discovery@latest", "check", url, timeout=60)
    if code != 0:
        return False, f"check a échoué: {output[:300]}"
    if "paid" not in output.lower():
        return False, f"pas reconnue comme payante: {output[:300]}"
    return True, output.strip()[:300]


async def check_cdp_validate(url: str, method: str) -> tuple[bool, str]:
    from cdp import CdpClient
    from cdp.openapi_client.api.x402_facilitator_api import X402FacilitatorApi
    from cdp.openapi_client.models.x402_validate_request import X402ValidateRequest

    if not (config.CDP_API_KEY_ID and config.CDP_API_KEY_SECRET):
        return False, "CDP_API_KEY_ID/SECRET non configurés"

    cdp = CdpClient(api_key_id=config.CDP_API_KEY_ID, api_key_secret=config.CDP_API_KEY_SECRET)
    try:
        api = X402FacilitatorApi(cdp.cdp_api_client)
        # *_without_preload_content: the SDK's typed response model has
        # drifted from the live API (a real "url_valid" preflight check
        # isn't in its enum yet) - parse the raw JSON ourselves rather than
        # let a strict pydantic model reject a response the API itself
        # considers valid.
        resp = await api.validate_x402_resource_without_preload_content(
            X402ValidateRequest(resource=url, method=method)
        )
        body = json.loads(await resp.read())
    except Exception as exc:
        return False, f"appel CDP validate échoué: {exc}"
    finally:
        await cdp.close()

    valid = body.get("valid")
    outcome = body.get("simulation", {}).get("outcome")
    if valid and outcome == "accepted":
        return True, f"valid={valid}, outcome={outcome}"
    failed_checks = [c for c in body.get("preflight", []) if not c.get("passed")]
    return False, f"valid={valid}, outcome={outcome}, échecs={failed_checks[:3]}"


async def check_probe_5methods(url: str) -> tuple[bool, str]:
    results = {}
    async with httpx.AsyncClient(timeout=15.0) as client:
        for method in PROBE_METHODS:
            try:
                resp = await client.request(method, url)
                results[method] = resp.status_code
            except Exception as exc:
                results[method] = f"erreur: {exc}"
    server_errors = {m: c for m, c in results.items() if isinstance(c, int) and c >= 500}
    if server_errors:
        return False, f"5xx détecté: {server_errors} (tout: {results})"
    return True, str(results)


async def check_sample(sample_url: str) -> tuple[bool, str]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            resp = await client.get(sample_url)
        except Exception as exc:
            return False, f"requête échouée: {exc}"
    if resp.status_code != 200:
        return False, f"status {resp.status_code}"
    try:
        body = resp.json()
    except Exception:
        return False, "réponse non-JSON"
    if not body:
        return False, "réponse vide"
    return True, f"status 200, {len(json.dumps(body))} octets"


async def check_header_size(url: str, method: str) -> tuple[bool, str]:
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            resp = await client.request(method, url)
        except Exception as exc:
            return False, f"requête échouée: {exc}"
    header_value = resp.headers.get("payment-required", "")
    size = len(header_value.encode())
    if size == 0:
        return False, "aucun en-tête payment-required (route protégée par x402 attendue)"
    if size > HEADER_SIZE_WARN_BYTES:
        return False, f"{size} octets - dépasse le seuil d'alerte de {HEADER_SIZE_WARN_BYTES}"
    return True, f"{size} octets"


async def test_route(slug: str, method: str, price: str) -> bool:
    base = config.BASE_URL.rstrip("/")
    url = f"{base}/{slug}"
    sample_url = f"{url}/sample"

    checks = [
        ("check", *check_route_discover(url)),
    ]

    cdp_ok, cdp_detail = await check_cdp_validate(url, method)
    checks.append(("cdp_validate", cdp_ok, cdp_detail))

    probe_ok, probe_detail = await check_probe_5methods(url)
    checks.append(("probe_5methods", probe_ok, probe_detail))

    sample_ok, sample_detail = await check_sample(sample_url)
    checks.append(("sample", sample_ok, sample_detail))

    header_ok, header_detail = await check_header_size(url, method)
    checks.append(("header_size", header_ok, header_detail))

    for check_name, passed, detail in checks:
        db.record_route_check(slug, check_name, passed, detail)

    all_passed = all(passed for _, passed, _ in checks)
    if not all_passed:
        failed = [f"{name}: {detail}" for name, passed, detail in checks if not passed]
        db.add_journal_entry(
            "controleur", "rejected", "; ".join(failed)[:500], route=slug
        )
    return all_passed


def _agentcash_finds_us(query: str) -> tuple[bool, str]:
    """Real `agentcash search`, not `discover`/`check` (those only prove our
    OpenAPI is well-formed, never that anyone can find us). Matches only
    against each result's real origin.url - a blind substring match on the
    raw output was tried first and produced a false positive: the response
    JSON echoes the query string back in its own "query" field, so searching
    for our own domain always "found" it there even when every listed
    origin.url belonged to someone else (caught 2026-09-06 by inspecting a
    "found" row that actually pointed at animica.dev)."""
    code, output = _npx("agentcash@latest", "search", query, timeout=60)
    if code != 0:
        return False, f"recherche échouée (code {code}): {output[:300]}"
    try:
        start = output.index("{")
        parsed, _ = json.JSONDecoder().raw_decode(output[start:])
        results = parsed["data"]["results"]["results"]
        origins = [r.get("origin", {}).get("url", "") for r in results]
    except (ValueError, KeyError, TypeError) as exc:
        return False, f"réponse illisible ({exc}): {output[:300]}"
    matches = [u for u in origins if OWN_DOMAIN in u]
    if matches:
        return True, f"trouvé: {matches}"
    return False, f"absent de {len(origins)} résultat(s)"


def check_bazaar_presence() -> tuple[bool, str]:
    """Reads the nightly catalogue_snapshot.json (scripts/catalogue_sync.py,
    01:00 cron, runs before this script's 02:00 cron) instead of re-paginating
    ~15 600 Bazaar resources ourselves - the CDP discovery API has no
    server-side filter param (confirmed empirically: resource/query/search
    params are all silently ignored), so a fresh full scan just for our own
    presence would be pure waste on top of what already runs nightly."""
    if not CATALOGUE_SNAPSHOT_PATH.exists():
        return False, "catalogue_snapshot.json absent - catalogue_sync.py a-t-il tourné cette nuit ?"
    try:
        snapshot = json.loads(CATALOGUE_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        return False, f"catalogue_snapshot.json illisible: {exc}"
    matches = [r for r in snapshot.get("resources", []) if OWN_DOMAIN in (r.get("resource") or "")]
    if matches:
        return True, f"{len(matches)} ressource(s): {[m['resource'] for m in matches][:5]}"
    return False, f"absent de {snapshot.get('total_resources', '?')} ressources (snapshot du {snapshot.get('generated_at', '?')})"


def _should_run_index_checks() -> bool:
    last = db.last_index_check_at()
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
    except ValueError:
        return True
    return datetime.now(timezone.utc) - last_dt >= timedelta(hours=INDEX_CHECK_INTERVAL_HOURS)


def run_index_visibility_checks() -> None:
    """Answers the question nothing else in this project answers: not "did
    registration succeed" but "can an agent actually find us". Gated to
    ~daily (see _should_run_index_checks) since this shells out to a real
    npx process 4 times - not something to do on every loop cycle."""
    found, detail = _agentcash_finds_us(OWN_DOMAIN)
    db.record_index_check("agentcash", OWN_DOMAIN, found, detail)

    for keyword in KIT_KEYWORDS:
        found, detail = _agentcash_finds_us(keyword)
        db.record_index_check("agentcash", keyword, found, detail)

    found, detail = check_bazaar_presence()
    db.record_index_check("bazaar", "_presence", found, detail)


async def run(route: str | None = None, trigger: str = "cron") -> dict:
    """The reusable core: both the CLI entrypoint below and the /admin/usine
    "Lancer" button (app/admin.py, in-process asyncio task) call this same
    function, so a manual trigger runs exactly the same checks as the cron."""
    start = time.monotonic()
    origin = config.BASE_URL.rstrip("/")

    discover_ok, discover_detail = check_discover(origin)
    db.record_route_check("_origin", "discover", discover_ok, discover_detail)

    route_configs = build_route_configs()
    slugs = []
    for route_key, route_config in route_configs.items():
        method, path = route_key.split(" ", 1)
        slug = path.lstrip("/")
        if route and slug != route:
            continue
        price = route_config.accepts.price if not isinstance(route_config.accepts, list) else route_config.accepts[0].price
        slugs.append((slug, method, price))

    if route and not slugs:
        return {"tested": 0, "rejected": 0, "summary": f"route inconnue: {route}", "ok": False}

    rejected = 0
    results = []
    for slug, method, price in slugs:
        route_ok = await test_route(slug, method, price)
        if not discover_ok:
            db.record_route_check(slug, "discover", False, discover_detail)
            route_ok = False
        if not route_ok:
            rejected += 1
        results.append((slug, route_ok))

    duration_ms = int((time.monotonic() - start) * 1000)
    summary = f"{len(slugs)} route(s) testée(s), {rejected} rejetée(s)"
    if not route:
        db.record_controleur_run(len(slugs), rejected, duration_ms, summary)
        db.add_journal_entry("controleur", "run", summary, trigger_kind=trigger)
        # Never on a single --route pre-publish check (Crieur calls this
        # mid-deploy, latency-sensitive) - only on a full pass, and even then
        # only ~daily (see _should_run_index_checks).
        if _should_run_index_checks():
            run_index_visibility_checks()
    return {"tested": len(slugs), "rejected": rejected, "summary": summary, "results": results, "ok": rejected == 0}


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--route", help="Ne tester qu'une seule route (slug), pour Crieur avant publication")
    args = parser.parse_args()

    result = await run(route=args.route)
    for slug, ok in result.get("results", []):
        print(f"{'OK' if ok else 'REJETÉ'}  {slug}")
    print(result["summary"])
    if args.route and result["tested"] == 0:
        sys.exit(1)
    if result["rejected"] > 0:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
