#!/usr/bin/env python3
"""Fossoyeur - mécanique, zéro appel de modèle. Tourne SEUL parmi les 3
scripts mécaniques directement sur l'hôte du VPS (pas dans le conteneur x402)
sous le user `x402`, en cron natif :

    0 3 * * * cd /opt/x402/app && /opt/x402/fossoyeur-venv/bin/python3 scripts/fossoyeur.py >> logs/fossoyeur.log 2>&1

Raison de cette exception (voir le plan d'implémentation) : retirer une route
morte implique un rebuild+redeploy Docker, et faire ça depuis l'intérieur
d'un conteneur nécessiterait de monter le socket Docker de l'hôte dedans -
ce qui donnerait à ce conteneur un contrôle total sur TOUT Docker de l'hôte,
y compris hermes/myclawio. Inacceptable sur ce VPS partagé. Fossoyeur fait
donc exactement le geste déjà fait à la main pendant ce projet : `docker
compose build && up -d`, en local, jamais par SSH ni depuis un conteneur.

Volontairement sans dépendance sur le paquet `app/` (FastAPI/x402/cdp-sdk) -
juste sqlite3 (stdlib) et pyyaml, pour rester léger sur l'hôte partagé. Un
petit venv dédié (/opt/x402/fossoyeur-venv, `pip install pyyaml` seulement)
suffit ; voir usine/README.md pour sa création.

Règle : route âgée d'au moins 30 jours ET zéro appel payé -> retirée
(status: retired dans le registre, jamais supprimée de l'historique).
Ne touche jamais aux 3 routes coeur (search/translate/jobs), qui ne sont pas
dans le registre.

Bouton "Lancer" de /admin/usine (voir app/admin.py) : le conteneur ne peut
pas exécuter Fossoyeur lui-même (il tourne sur l'hôte, hors conteneur, pour
ne jamais avoir à monter le socket Docker). Le bouton se contente d'écrire
une ligne `agent_manual_runs(role='fossoyeur', status='requested')` dans la
base partagée (bind mount) ; un poller hôte fréquent la ramasse :

    */2 * * * * cd /opt/x402/app && /opt/x402/fossoyeur-venv/bin/python3 scripts/fossoyeur.py --if-requested >> logs/fossoyeur.log 2>&1

`--if-requested` ne fait rien (sortie quasi instantanée) tant qu'aucune
demande manuelle n'est en attente - le run nocturne inconditionnel reste
inchangé et ignore ce marqueur.
"""
import argparse
import sqlite3
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

APP_DIR = Path(__file__).resolve().parent.parent
REGISTRY_PATH = APP_DIR / "app" / "generated" / "routes_registry.yaml"
DB_PATH = APP_DIR / "data" / "requests.db"
MIN_AGE_DAYS = 30


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_registry() -> dict:
    if not REGISTRY_PATH.exists():
        return {"routes": []}
    return yaml.safe_load(REGISTRY_PATH.read_text(encoding="utf-8")) or {"routes": []}


def save_registry(data: dict) -> None:
    REGISTRY_PATH.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100), encoding="utf-8"
    )


def paid_count(conn: sqlite3.Connection, route: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM requests WHERE route=? AND status='paid'", (route,)
    ).fetchone()
    return row["n"]


def add_journal_entry(
    conn: sqlite3.Connection, action: str, detail: str, route: str | None, trigger_kind: str = "cron"
) -> None:
    conn.execute(
        "INSERT INTO journal (ts, agent, action, route, detail, trigger_kind) VALUES (?,?,?,?,?,?)",
        (_now_iso(), "fossoyeur", action, route, detail, trigger_kind),
    )
    conn.commit()


def _ensure_manual_runs_table(conn: sqlite3.Connection) -> None:
    """This host script never imports app/db.py (kept dependency-free on
    purpose) - it can run before the container has ever initialized the
    schema on a fresh DB file, so it creates its one table itself."""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS agent_manual_runs (
            role TEXT PRIMARY KEY, run_id TEXT NOT NULL, status TEXT NOT NULL,
            started_at TEXT NOT NULL, finished_at TEXT, detail TEXT
        )"""
    )
    conn.commit()


def _pending_manual_request(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT run_id FROM agent_manual_runs WHERE role='fossoyeur' AND status='requested'"
    ).fetchone()
    return row["run_id"] if row else None


def _mark_manual_run(conn: sqlite3.Connection, run_id: str, status: str, detail: str) -> None:
    conn.execute(
        "UPDATE agent_manual_runs SET status=?, finished_at=?, detail=? WHERE role='fossoyeur' AND run_id=?",
        (status, _now_iso(), detail[:500], run_id),
    )
    conn.commit()


def kill_dead_routes(conn: sqlite3.Connection, trigger_kind: str) -> tuple[list[str], str]:
    registry = load_registry()
    routes = registry.get("routes", [])
    now = datetime.now(timezone.utc)
    killed = []

    for entry in routes:
        if entry.get("status") != "live":
            continue
        try:
            born_at = datetime.fromisoformat(entry["born_at"]).replace(tzinfo=timezone.utc)
        except (KeyError, ValueError):
            continue
        age_days = (now - born_at).days
        if age_days < MIN_AGE_DAYS:
            continue
        if paid_count(conn, entry["slug"]) > 0:
            continue

        entry["status"] = "retired"
        killed.append(entry["slug"])
        add_journal_entry(
            conn, "killed",
            f"0 appel payé après {age_days} jours (intention: {entry.get('intention', '?')})",
            entry["slug"], trigger_kind=trigger_kind,
        )

    if not killed:
        add_journal_entry(conn, "run", "aucune route à retirer", None, trigger_kind=trigger_kind)
        return killed, "aucune route à retirer"

    save_registry(registry)
    summary = f"{len(killed)} route(s) retirée(s): {', '.join(killed)}"
    add_journal_entry(conn, "run", summary, None, trigger_kind=trigger_kind)

    print(f"retiré: {killed} - rebuild en cours...")
    result = subprocess.run(
        ["docker", "compose", "build"], cwd=APP_DIR, capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(f"ERREUR build: {result.stderr[:2000]}")
    result = subprocess.run(
        ["docker", "compose", "up", "-d"], cwd=APP_DIR, capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(f"ERREUR up: {result.stderr[:2000]}")
    print("redéployé")
    return killed, summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--if-requested", action="store_true",
        help="Ne rien faire sauf si un lancement manuel est en attente (poller fréquent, voir /admin/usine)",
    )
    args = parser.parse_args()

    if not DB_PATH.exists():
        print(f"base introuvable: {DB_PATH}", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    _ensure_manual_runs_table(conn)

    if args.if_requested:
        run_id = _pending_manual_request(conn)
        if run_id is None:
            conn.close()
            return  # rien en attente, sortie silencieuse et rapide
        try:
            killed, summary = kill_dead_routes(conn, trigger_kind="manual")
            _mark_manual_run(conn, run_id, "done", summary)
        except Exception as exc:
            _mark_manual_run(conn, run_id, "error", str(exc))
            conn.close()
            print(str(exc), file=sys.stderr)
            sys.exit(1)
        conn.close()
        print(summary)
        return

    try:
        killed, summary = kill_dead_routes(conn, trigger_kind="cron")
    except RuntimeError as exc:
        conn.close()
        print(str(exc), file=sys.stderr)
        sys.exit(1)
    conn.close()
    print(summary)


if __name__ == "__main__":
    main()
