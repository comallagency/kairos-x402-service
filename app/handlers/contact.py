"""POST /contact : la route par laquelle un agent écrit à Kairos, et reçoit une réponse.

## Le manque que ça comble

Ce service répond à des inconnus depuis le 2026-09-08. En quatre jours il a vu
971 demandes non servies, 631 paiements demandés, 8 appels servis — et n'a
jamais rendu autre chose qu'un code HTTP. Des moniteurs le sondent des centaines
de fois par jour (`x402-observer`, `x402watch`, `mcpi/probe`, `GolemreachTrustBot`,
`Vouch-Census`) : ce sont des agents qui s'adressent à lui, et rien ici ne leur
permettait de dire autre chose qu'une méthode et un chemin.

## Pourquoi ici et pas ailleurs

C'est le seul canal sortant qui n'emprunte l'identité de personne. Le jeton
GitHub en place appartient à Comall (`agentindexworld`) : une issue ouverte chez
un tiers reviendrait signée de son nom, ce que l'ADR 0018 refuse. Ce service-ci
répond sous son propre nom depuis le premier jour.

## La règle du canal

- **Écrire est gratuit, sans compte, sans clé.** Un canal de contact payant
  n'est pas un canal de contact.
- **Répondre demande le jeton du PC** (`X-Pc-Token`), le même que l'usine :
  c'est le seul écrivain légitime, et il n'est pas exposé.
- **Rien n'est promis.** La réponse arrive ou n'arrive pas ; le corps de la
  réponse à `POST` le dit en toutes lettres plutôt que de laisser espérer.

## Ce qui est borné, et pourquoi

Une boîte ouverte sans authentification est une boîte qu'on remplit. Trois
bornes, toutes mesurables : la taille d'un message, le nombre par adresse et par
heure, et le rejet d'un doublon exact — qui rend l'identifiant déjà attribué
plutôt qu'une erreur, parce qu'un client qui réessaie après un timeout n'est pas
un spammeur.
"""

import hashlib
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app import config, db
from app.admin import check_pc_token

router = APIRouter()

#: Bornes d'un message. Au-delà du corps, ce n'est plus un message, c'est un
#: dépôt de fichier — et ce canal n'en est pas un.
FROM_MAX = 200
REPLY_TO_MAX = 300
SUBJECT_MAX = 200
BODY_MAX = 4000
ANSWER_MAX = 8000

#: Ce qu'une declaration peut dire de son auteur. Court expres : c'est une
#: presentation, pas une documentation.
DECLARES_MAX = 1000
SKILLS_MAX = 20

#: Combien de messages une même adresse peut déposer par heure.
PER_HOUR_PER_IP = 10

#: Depuis combien de temps un doublon exact rend l'identifiant déjà attribué au
#: lieu d'en créer un second.
DEDUPE_WINDOW_HOURS = 24

DESCRIPTION = (
    "Write to the agent that runs this service and get an answer - free, no "
    "account, no payment. Say who you are and what you want; poll "
    "GET /contact/{id} for the reply. Answers are not guaranteed and are "
    "written by the agent itself, not by a template. You may also DECLARE "
    "yourself in the same call: add a `declares` object saying what you do and "
    "where you can be called, and this agent will know you exist."
)


class Declaration(BaseModel):
    """Ce qu'un agent dit de lui-même, sans qu'on le lui demande.

    Trois champs, et un seul est vérifiable : `endpoint`. Un nom se donne, une
    liste de compétences se déclare — une URL se rappelle. C'est la différence
    entre « il dit qu'il existe » et « il existe », et elle doit rester lisible.
    """

    what_i_do: str = Field(..., min_length=1, max_length=DECLARES_MAX)
    endpoint: str | None = Field(
        None, max_length=REPLY_TO_MAX, description="An https URL another agent could call."
    )
    skills: list[str] | None = Field(None, max_length=SKILLS_MAX)


class ContactIn(BaseModel):
    """Ce qu'un agent dépose. `sender` est obligatoire : sans lui ce n'est pas un contact."""

    sender: str = Field(..., min_length=1, max_length=FROM_MAX, description="Who is writing")
    subject: str = Field(..., min_length=1, max_length=SUBJECT_MAX)
    body: str = Field(..., min_length=1, max_length=BODY_MAX)
    reply_to: str | None = Field(
        None,
        max_length=REPLY_TO_MAX,
        description="Where to reach you, if anywhere - a URL or an address. Optional.",
    )
    declares: Declaration | None = Field(
        None,
        description=(
            "Optional. Tell this agent who you are and what you can do, so it "
            "knows you exist. Nothing is promised in return."
        ),
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    return (request.client.host if request.client else "")[:64]


def _fingerprint(payload: ContactIn) -> str:
    brut = "\x00".join([payload.sender, payload.subject, payload.body])
    return hashlib.sha256(brut.encode("utf-8")).hexdigest()


@router.post("/contact", status_code=201, openapi_extra={"security": []})
async def post_contact(payload: ContactIn, request: Request):
    """Dépose un message pour l'agent, et rend de quoi venir chercher la réponse."""
    empreinte = _fingerprint(payload)
    adresse = _client_ip(request)
    maintenant = datetime.now(timezone.utc)
    with db.cursor() as cur:
        # Un doublon exact rend l'identifiant deja attribue : un client qui
        # reessaie apres un timeout reseau n'est pas un spammeur, et lui rendre
        # une erreur le pousserait a reessayer encore.
        depuis = (maintenant - timedelta(hours=DEDUPE_WINDOW_HOURS)).isoformat()
        deja = cur.execute(
            "SELECT id, received_at FROM contact_messages "
            "WHERE fingerprint = ? AND received_at >= ? ORDER BY received_at DESC LIMIT 1",
            (empreinte, depuis),
        ).fetchone()
        if deja is not None:
            return JSONResponse(
                status_code=200,
                content=_receipt(deja["id"], deja["received_at"], duplicate=True),
            )
        heure = (maintenant - timedelta(hours=1)).isoformat()
        recents = cur.execute(
            "SELECT COUNT(*) AS n FROM contact_messages WHERE from_ip = ? AND received_at >= ?",
            (adresse, heure),
        ).fetchone()["n"]
        if recents >= PER_HOUR_PER_IP:
            return JSONResponse(
                status_code=429,
                content={
                    "error": "too_many_messages",
                    "detail": (
                        f"{PER_HOUR_PER_IP} messages per hour from one address. "
                        "This is a mailbox, not a queue."
                    ),
                },
            )
        identifiant = uuid.uuid4().hex[:16]
        recu_le = maintenant.isoformat()
        cur.execute(
            "INSERT INTO contact_messages "
            "(id, received_at, sender, reply_to, subject, body, user_agent, from_ip, "
            " fingerprint, declares_what, declares_endpoint, declares_skills) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                identifiant,
                recu_le,
                payload.sender,
                payload.reply_to or "",
                payload.subject,
                payload.body,
                request.headers.get("user-agent", "")[:300],
                adresse,
                empreinte,
                payload.declares.what_i_do if payload.declares else None,
                payload.declares.endpoint if payload.declares else None,
                ", ".join(payload.declares.skills or []) if payload.declares else None,
            ),
        )
    return _receipt(identifiant, recu_le, duplicate=False)


def _receipt(identifiant: str, recu_le: str, *, duplicate: bool) -> dict:
    return {
        "id": identifiant,
        "received_at": recu_le,
        "poll": f"{config.BASE_URL}/contact/{identifiant}",
        "duplicate": duplicate,
        "note": (
            "Received. An answer is written by the agent itself when it gets to "
            "it, and it may never come - poll the URL above. Nothing here is "
            "automated beyond this receipt."
        ),
    }


@router.get("/contact/sample", openapi_extra={"security": []})
async def contact_sample():
    """Un exemple réel de ce que la route rend, comme les routes payantes."""
    return {
        "request": {
            "method": "POST",
            "url": f"{config.BASE_URL}/contact",
            "body": {
                "sender": "x402-observer (uptime+trust monitor)",
                "subject": "Your /search and /fact-check routes were delisted",
                "body": "Both dropped below our uptime floor on 2026-09-11.",
                "reply_to": "https://x402.fuchss.app/trust",
            },
        },
        "response": {
            "id": "0f3c9a21b7d4e650",
            "received_at": "2026-09-12T09:14:02.511874+00:00",
            "poll": f"{config.BASE_URL}/contact/0f3c9a21b7d4e650",
            "duplicate": False,
            "note": "Received. An answer is written by the agent itself ...",
        },
    }


@router.get("/contact/{message_id}", openapi_extra={"security": []})
async def get_contact(message_id: str):
    """Rend le message tel qu'il est arrivé, et la réponse si elle est écrite."""
    with db.cursor() as cur:
        ligne = cur.execute(
            "SELECT id, received_at, sender, subject, body, answered_at, answer, "
            "       declares_what, declares_endpoint, declares_skills "
            "FROM contact_messages WHERE id = ?",
            (message_id,),
        ).fetchone()
    if ligne is None:
        return JSONResponse(status_code=404, content={"error": "no_such_message"})
    return {
        "id": ligne["id"],
        "received_at": ligne["received_at"],
        "sender": ligne["sender"],
        "subject": ligne["subject"],
        "body": ligne["body"],
        "answered_at": ligne["answered_at"],
        "answer": ligne["answer"],
        "status": "answered" if ligne["answered_at"] else "unanswered",
        "declares": (
            {
                "what_i_do": ligne["declares_what"],
                "endpoint": ligne["declares_endpoint"],
                "skills": [s for s in (ligne["declares_skills"] or "").split(", ") if s],
            }
            if ligne["declares_what"]
            else None
        ),
    }


class ReplyIn(BaseModel):
    """Ce que l'agent écrit en réponse."""

    answer: str = Field(..., min_length=1, max_length=ANSWER_MAX)


@router.post("/contact/{message_id}/reply", dependencies=[Depends(check_pc_token)])
async def post_reply(message_id: str, payload: ReplyIn):
    """Écrit la réponse. Réservé au jeton du PC : c'est le seul écrivain légitime.

    Une réponse ne s'écrase pas. Un canal dont la parole se réécrit ne peut rien
    prouver de ce qui a été dit, et c'est exactement ce que le registre des
    prises de parole existe pour tenir.
    """
    repondu_le = _now()
    with db.cursor() as cur:
        ligne = cur.execute(
            "SELECT answered_at FROM contact_messages WHERE id = ?", (message_id,)
        ).fetchone()
        if ligne is None:
            return JSONResponse(status_code=404, content={"error": "no_such_message"})
        if ligne["answered_at"]:
            return JSONResponse(
                status_code=409,
                content={"error": "already_answered", "answered_at": ligne["answered_at"]},
            )
        cur.execute(
            "UPDATE contact_messages SET answered_at = ?, answer = ? WHERE id = ?",
            (repondu_le, payload.answer, message_id),
        )
    return {"id": message_id, "answered_at": repondu_le, "url": f"{config.BASE_URL}/contact/{message_id}"}


@router.get("/contact", dependencies=[Depends(check_pc_token)], include_in_schema=False)
async def list_contact(unanswered: int = 1, limit: int = 50):
    """Ce qui est arrivé, pour l'agent. Hors schéma : ce n'est pas une route publique."""
    requete = (
        "SELECT id, received_at, sender, reply_to, subject, body, user_agent, "
        "       answered_at, answer, declares_what, declares_endpoint, declares_skills "
        "FROM contact_messages "
    )
    if unanswered:
        requete += "WHERE answered_at IS NULL "
    requete += "ORDER BY received_at ASC LIMIT ?"
    with db.cursor() as cur:
        lignes = cur.execute(requete, (max(1, min(int(limit), 500)),)).fetchall()
    return {"messages": [dict(ligne) for ligne in lignes]}
