#!/usr/bin/env python3
"""Garde-fou pour /place : sauvegarde ce qui est publié, restaure ce qui a disparu.

Contexte : la table `place` a été perdue silencieusement entre le 02/10 et le
20/10 2026 (seul 1 document sur 8 restait), et rien ne l'avait signalé — elle
n'a été redécouverte que 740h plus tard. Rien n'automatisait une republication
après une perte, qu'elle vienne d'un recreate de conteneur ou d'autre chose.

Ce script lit ce qui est publié maintenant (GET /place, gratuit et public),
le compare à la dernière sauvegarde connue (data/place_backup.json, dans le
bind mount — survit à un recreate de conteneur), et :
  - si un slug de la sauvegarde a disparu du live, le republie (PUT, avec le
    jeton du PC lu dans .env) ;
  - rafraîchit ensuite la sauvegarde avec l'état live (ce qui inclut toute
    nouvelle publication faite depuis).

Lancé en fin de `scripts/deploy.sh` (le recreate qu'on cherchait à couvrir) et
par cron quotidien (toute autre cause de perte).
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
ENV_FILE = APP_DIR / ".env"
BACKUP_FILE = APP_DIR / "data" / "place_backup.json"


def _lire_env(cle: str) -> str:
    for ligne in ENV_FILE.read_text(encoding="utf-8").splitlines():
        if ligne.startswith(f"{cle}="):
            return ligne.split("=", 1)[1].strip()
    raise SystemExit(f"{cle} absent de {ENV_FILE}")


def _get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _put(url: str, jeton: str, titre: str, corps: str) -> None:
    requete = urllib.request.Request(
        url,
        method="PUT",
        data=json.dumps({"title": titre, "body": corps}).encode("utf-8"),
        headers={"content-type": "application/json", "X-Pc-Token": jeton},
    )
    with urllib.request.urlopen(requete, timeout=15) as resp:
        resp.read()


def main() -> int:
    base_url = _lire_env("BASE_URL")
    jeton = _lire_env("PC_LISTENER_TOKEN")

    live = _get(f"{base_url}/place")
    live_slugs = {d["slug"] for d in live["documents"]}

    sauvegarde: dict[str, dict] = {}
    if BACKUP_FILE.is_file():
        sauvegarde = json.loads(BACKUP_FILE.read_text(encoding="utf-8"))

    manquants = [slug for slug in sauvegarde if slug not in live_slugs]
    for slug in manquants:
        doc = sauvegarde[slug]
        try:
            _put(f"{base_url}/place/{slug}", jeton, doc["title"], doc["body"])
            print(f"restauré : {slug}")
        except urllib.error.URLError as souci:
            print(f"ÉCHEC restauration {slug} : {souci}", file=sys.stderr)

    # Rafraîchit la sauvegarde avec l'état live (après restauration éventuelle).
    live_apres = _get(f"{base_url}/place") if manquants else live
    nouvelle_sauvegarde = dict(sauvegarde)
    for resume in live_apres["documents"]:
        slug = resume["slug"]
        corps_complet = urllib.request.urlopen(
            f"{base_url}/place/{slug}", timeout=15
        ).read().decode("utf-8")
        # Le GET rend du Markdown rendu (titre + corps + pied de page, voir
        # handlers/place.py:lire_un_document) ; on retire ce que la route
        # elle-même ajoute pour ne garder que le corps original.
        _, _, reste = corps_complet.partition("\n\n")
        pied = f"\n\n---\n_{slug} — "
        corps = reste.rsplit(pied, 1)[0] if pied in reste else reste
        nouvelle_sauvegarde[slug] = {"title": resume["title"], "body": corps}

    BACKUP_FILE.parent.mkdir(parents=True, exist_ok=True)
    BACKUP_FILE.write_text(
        json.dumps(nouvelle_sauvegarde, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"sauvegarde à jour : {len(nouvelle_sauvegarde)} document(s), {len(manquants)} restauré(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
