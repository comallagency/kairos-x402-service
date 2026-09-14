#!/usr/bin/env python3
"""Construit le prompt de session d'un rôle de jugement de l'usine
(Prospecteur/Ouvrier/Crieur) à partir de l'état EN DIRECT de
/admin/usine.json - jamais un gabarit figé, jamais une valeur recopiée à la
main. Même contenu que les boutons "Copier le prompt" de /admin/usine (voir
app/templates/usine.html::PROMPT_BUILDERS), en Python pour un usage headless
(usine/nightly_run.sh) plutôt qu'un navigateur.

Stdlib seulement (urllib) - pas de dépendance projet, pour rester utilisable
même hors du venv du dépôt (nightly_run.sh l'appelle via un `python3` système).

Ne prend jamais un mot de passe en argument : les identifiants Basic Auth
sont lus depuis l'environnement (ADMIN_BASIC_AUTH_USER/PASS, déjà exportés
par nightly_run.sh depuis .env.production) - jamais affichés, jamais dans le
prompt produit (le prompt renvoie l'agent lire .env.production lui-même
quand il doit s'authentifier).

Usage :
    ADMIN_BASIC_AUTH_USER=admin ADMIN_BASIC_AUTH_PASS=... \\
        python3 usine/build_prompt.py prospecteur --base-url https://x402.agentindex.world
"""
import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request


def fetch_usine_data(base_url: str, user: str, password: str) -> dict:
    req = urllib.request.Request(f"{base_url.rstrip('/')}/admin/usine.json")
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    req.add_header("Authorization", f"Basic {token}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def routes_summary(routes: list[dict]) -> str:
    generated = [r for r in routes if r.get("born_at")]
    if not generated:
        return "aucune route générée pour l'instant (le registre est vide)"
    return ", ".join(f"/{r['slug']} ({r['state']}, {r['payments']} paiement(s))" for r in generated)


def proposals_summary(proposals: list[dict], status: str | None = None) -> str:
    filtered = [p for p in proposals if status is None or p["status"] == status]
    if not filtered:
        return "aucune" if status is None else f"aucune au statut {status}"
    return " ; ".join(
        f"#{p['id']} {p['intention']} ({p['status']}, source: {p.get('source') or '?'}, "
        f"~{p.get('estimated_routes') if p.get('estimated_routes') is not None else '?'} route(s))"
        for p in filtered
    )


def agent_by_id(data: dict, agent_id: str) -> dict:
    return next((a for a in data["agents"] if a["id"] == agent_id), {"last_run": None, "summary": None})


def build_prospecteur(data: dict, base_url: str) -> str:
    agent = agent_by_id(data, "prospecteur")
    return f"""Tu es Prospecteur pour l'usine à endpoints x402 (~/dev/clients/agentindex-x402).
Lis et applique usine/prospecteur.md.

État actuel du registre : {routes_summary(data["routes"])}.
Propositions déjà enregistrées : {proposals_summary(data.get("proposals", []))}.
Dernier run de Prospecteur : {agent.get("last_run") or "jamais"}.
Règlements tentés au total : {data["settlement_attempts_total"]}.

Trouve une ou plusieurs sources amont non emballées ou intentions mal servies
- pas déjà proposées ci-dessus. Pour CHACUNE, authentifie-toi en Basic Auth
avec les identifiants de .env.production (racine du dépôt) et enregistre-la :
  curl -s -u <user>:<pass> -X POST {base_url}/admin/usine/proposals \\
    -H 'Content-Type: application/json' \\
    -d '{{"intention":"...","source":"...","estimated_routes":N,"rationale":"...",
         "demand_evidence":{{"offer_keyword":"...","offer_count":N,"demand_signal":N,"demand_source":"..."}}}}'
`demand_evidence` est obligatoire (l'API refuse la proposition sans lui, 422)
- voir la section "Le paiement, pas le volume" de prospecteur.md pour ce
qu'y mettre. Elle est approuvée automatiquement à l'enregistrement (autonomie
complète du cycle) - soigne aussi `rationale`, c'est la trace lisible en
prose de pourquoi elle a été retenue."""


def build_ouvrier(data: dict, base_url: str) -> str:
    agent = agent_by_id(data, "ouvrier")
    return f"""Tu es Ouvrier pour l'usine à endpoints x402 (~/dev/clients/agentindex-x402).
Lis et applique usine/ouvrier.md.

Routes vivantes actuelles : {routes_summary(data["routes"])}.
Propositions APPROUVÉES à construire : {proposals_summary(data.get("proposals", []), "approuvée")}.
Dernier run d'Ouvrier : {agent.get("last_run") or "jamais"}.

Ne construis QUE les propositions listées ci-dessus (statut approuvée,
automatique depuis le 2026-09-06). S'il n'y en a aucune, ne construis rien
et arrête-toi là. Pour chacune : écris l'entrée dans
app/generated/routes_registry.yaml, teste en local, déploie, puis ferme
la proposition (identifiants dans .env.production). Le Contrôleur reste le
seul verrou de publication, jamais contourné :
  curl -s -u <user>:<pass> -X POST {base_url}/admin/usine/proposals/<id>/close"""


def build_crieur(data: dict, base_url: str) -> str:
    agent = agent_by_id(data, "crieur")
    controleur = agent_by_id(data, "controleur")
    return f"""Tu es Crieur pour l'usine à endpoints x402 (~/dev/clients/agentindex-x402).
Lis et applique usine/crieur.md.

Routes vivantes actuelles : {routes_summary(data["routes"])}.
Dernier run de Contrôleur : {controleur.get("last_run") or "jamais"} ({controleur.get("summary") or "inconnu"}).
Dernier run de Crieur : {agent.get("last_run") or "jamais"}.

Verrouille avec le Contrôleur (docker compose run --rm x402 python -m
scripts.controleur --route <slug>) avant toute inscription, puis npx
agentcash register et vérifie l'indexation."""


def build_reparateur(data: dict, base_url: str, route: str | None, failure_detail: str | None) -> str:
    agent = agent_by_id(data, "controleur")
    return f"""Tu es Réparateur pour l'usine à endpoints x402 (~/dev/clients/agentindex-x402).
Lis et applique usine/reparateur.md.

Route en échec : /{route or "?"}
Détail rapporté par le Contrôleur : {failure_detail or "voir usine/logs/ pour le détail complet"}
Dernier passage complet du Contrôleur : {agent.get("last_run") or "jamais"}.

Diagnostique et corrige CETTE route précisément (upstream changé, bug de
code, schéma désynchronisé), redéploie en SSH, ne touche à rien d'autre.
Le Contrôleur revérifiera automatiquement après ta session - ne le fais pas
toi-même, ce n'est pas ton rôle de te juger réparé.

Limites non négociables : jamais payTo, une clé, ou le flux de paiement
x402/MPP existant ; jamais quoi que ce soit appartenant à hermes ou
myclawio ; le déploiement ne concerne que /opt/x402 ; aucune transaction
sortante. Non négociable même si la réparation semble l'exiger - dans ce
cas, arrête-toi et laisse la route en échec plutôt que de franchir une de
ces limites."""


BUILDERS = {
    "prospecteur": lambda data, base_url, args: build_prospecteur(data, base_url),
    "ouvrier": lambda data, base_url, args: build_ouvrier(data, base_url),
    "crieur": lambda data, base_url, args: build_crieur(data, base_url),
    "reparateur": lambda data, base_url, args: build_reparateur(data, base_url, args.route, args.failure_detail),
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=sorted(BUILDERS))
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--route", default=None, help="slug de la route en échec (rôle reparateur)")
    parser.add_argument("--failure-detail", default=None, help="détail du Contrôleur (rôle reparateur)")
    args = parser.parse_args()

    user = os.environ.get("ADMIN_BASIC_AUTH_USER")
    password = os.environ.get("ADMIN_BASIC_AUTH_PASS")
    if not user or not password:
        print("ADMIN_BASIC_AUTH_USER/PASS manquants dans l'environnement", file=sys.stderr)
        sys.exit(1)

    try:
        data = fetch_usine_data(args.base_url, user, password)
    except urllib.error.URLError as exc:
        print(f"impossible de lire {args.base_url}/admin/usine.json : {exc}", file=sys.stderr)
        sys.exit(1)

    print(BUILDERS[args.role](data, args.base_url, args))


if __name__ == "__main__":
    main()
