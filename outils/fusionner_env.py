#!/usr/bin/env python3
"""Fusionne un fichier .env patch dans un .env existant sans effacer les clés absentes du patch.

Cas d'usage : après un `docker compose up --force-recreate`, un script ne doit pas
réécrire `/opt/x402/app/.env` avec une seule variable (ex. PC_LISTENER_TOKEN) —
c'est ce qui a basculé LocalDevFacilitator / Sepolia le 18/09.

Sans `--ecrire`, affiche le résultat sur stdout (dry-run par défaut).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

_ASSIGN_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def _lire_assignations(chemin: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for ligne in chemin.read_text(encoding="utf-8").splitlines():
        s = ligne.strip()
        if not s or s.startswith("#"):
            continue
        m = _ASSIGN_RE.match(s)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def fusionner(
    existant: dict[str, str],
    patch: dict[str, str],
) -> dict[str, str]:
    """Applique le patch sur l'existant sans supprimer les clés absentes du patch."""
    fusion = dict(existant)
    fusion.update(patch)
    return fusion


def _ordonner_cles(existant: dict[str, str], fusion: dict[str, str]) -> list[str]:
    ordre: list[str] = []
    for k in existant:
        if k in fusion and k not in ordre:
            ordre.append(k)
    for k in sorted(fusion):
        if k not in ordre:
            ordre.append(k)
    return ordre


def formater(fusion: dict[str, str], ordre_cles: list[str]) -> str:
    """Sérialise les paires clé=valeur dans l'ordre demandé."""
    lignes = [f"{k}={fusion[k]}" for k in ordre_cles if k in fusion]
    return "\n".join(lignes) + "\n"


def valider_production(fusion: dict[str, str]) -> list[str]:
    """Retourne les erreurs bloquantes si ENVIRONMENT=production est incohérent."""
    if fusion.get("ENVIRONMENT", "").strip().lower() != "production":
        return []
    erreurs: list[str] = []
    pay_to = fusion.get("X402_PAY_TO", "").strip()
    if not pay_to or pay_to in {"0x0", "0x0000000000000000000000000000000000000000"}:
        erreurs.append("X402_PAY_TO vide ou nul en production")
    net = fusion.get("X402_NETWORK", "").strip().lower()
    if "sepolia" in net or net in {"testnet", "dev"}:
        erreurs.append(f"X402_NETWORK suspect en production: {fusion.get('X402_NETWORK')}")
    if not fusion.get("CDP_API_KEY_ID") or not fusion.get("CDP_API_KEY_SECRET"):
        erreurs.append("CDP_API_KEY_ID / CDP_API_KEY_SECRET requis en production")
    return erreurs


def main() -> None:
    """Point d'entrée CLI."""
    parser = argparse.ArgumentParser(
        description="Fusionne un patch .env sans écraser les clés existantes",
    )
    parser.add_argument("--cible", type=Path, required=True, help="Fichier .env à mettre à jour")
    parser.add_argument(
        "--patch",
        type=Path,
        required=True,
        help="Fichier .env partiel (nouvelles valeurs)",
    )
    parser.add_argument(
        "--ecrire",
        action="store_true",
        help="Écrire --cible (sinon stdout uniquement)",
    )
    parser.add_argument(
        "--exiger-production",
        action="store_true",
        help=(
            "Échoue si ENVIRONMENT=production est incohérent (payTo, réseau, CDP)"
        ),
    )
    args = parser.parse_args()

    if not args.cible.is_file():
        sys.stderr.write(f"cible introuvable: {args.cible}\n")
        sys.exit(2)
    if not args.patch.is_file():
        sys.stderr.write(f"patch introuvable: {args.patch}\n")
        sys.exit(2)

    existant = _lire_assignations(args.cible)
    patch = _lire_assignations(args.patch)
    fusion = fusionner(existant, patch)
    ordre = _ordonner_cles(existant, fusion)
    texte = formater(fusion, ordre)

    if args.exiger_production:
        erreurs = valider_production(fusion)
        if erreurs:
            for e in erreurs:
                sys.stderr.write(f"refus: {e}\n")
            sys.exit(1)

    if args.ecrire:
        args.cible.write_text(texte, encoding="utf-8")
    else:
        sys.stdout.write(texte)


if __name__ == "__main__":
    main()
