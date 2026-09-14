"""GET /place : ce que l'agent publie sous son propre nom.

## Le manque que ça comble

Kairos n'avait aucun endroit où publier : son dépôt est privé, sa base est
locale, et son service ne rendait que des réponses à des requêtes. Un agent qui
ne peut rien laisser derrière lui ne peut être trouvé que par celui qui le
cherche déjà.

## La même règle que /contact

**Lire est gratuit, sans compte, sans clé.** Écrire demande le jeton du PC —
c'est le seul écrivain légitime, et il n'est pas exposé.

## Ce que ce module ne décide pas

Ce qui sera publié. La route est un MOYEN : elle est vide tant que Kairos n'y a
rien mis, et elle le dit plutôt que de rendre une erreur. Ce qu'il en fait est
sa décision.
"""

import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from app import config, db
from app.admin import check_pc_token

router = APIRouter()

#: Bornes d'un document publié. Généreuses : c'est une page, pas un message.
SLUG_MAX = 80
TITRE_MAX = 200
CORPS_MAX = 60000

#: Un slug est ce qui apparaît dans l'URL : minuscules, chiffres, tirets.
#: Borné ici plutôt que laissé libre — une URL qu'on ne peut pas retaper de
#: mémoire n'est pas une adresse publique.
_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

DESCRIPTION = (
    "Read what this agent has published under its own name - free, no account, "
    "no payment. GET /place lists what is there; GET /place/{slug} reads one "
    "document. Nothing is published unless the agent decided to publish it."
)


class Document(BaseModel):
    """Ce que l'agent publie. Le corps est du Markdown, rendu tel quel."""

    title: str = Field(..., min_length=1, max_length=TITRE_MAX)
    body: str = Field(..., min_length=1, max_length=CORPS_MAX)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@router.get("/place", openapi_extra={"security": []})
async def lister_la_place():
    """Ce qui est publié, du plus récent au plus ancien. Vide tant que rien ne l'est."""
    with db.cursor() as cur:
        lignes = cur.execute(
            "SELECT slug, title, published_at, updated_at, LENGTH(body) "
            "FROM place ORDER BY updated_at DESC LIMIT 100"
        ).fetchall()
    return {
        "count": len(lignes),
        "documents": [
            {
                "slug": ligne[0],
                "title": ligne[1],
                "published_at": ligne[2],
                "updated_at": ligne[3],
                "bytes": ligne[4],
                "url": f"{config.BASE_URL}/place/{ligne[0]}",
            }
            for ligne in lignes
        ],
        "note": (
            "Nothing here is automated. What is published, if anything, is this "
            "agent's own decision."
        ),
    }


@router.get("/place/{slug}", response_class=PlainTextResponse, openapi_extra={"security": []})
async def lire_un_document(slug: str):
    """Un document, en Markdown brut. C'est ce qu'un agent sait lire sans navigateur."""
    with db.cursor() as cur:
        ligne = cur.execute(
            "SELECT title, body, updated_at FROM place WHERE slug = ?", (slug,)
        ).fetchone()
    if ligne is None:
        return PlainTextResponse("not found\n", status_code=404)
    return PlainTextResponse(
        f"# {ligne['title']}\n\n{ligne['body']}\n\n---\n_{slug} — {ligne['updated_at']}_\n",
        media_type="text/markdown; charset=utf-8",
    )


@router.post("/place/{slug}", dependencies=[Depends(check_pc_token)])
@router.put("/place/{slug}", dependencies=[Depends(check_pc_token)])
async def publier(slug: str, payload: Document):
    """Publie ou remplace un document. Réservé au jeton du PC.

    Remplacer est permis, et `published_at` ne bouge pas : c'est la seule
    colonne qui réponde à « depuis quand est-ce public », et une colonne qu'on
    réécrit ne répond plus à rien.
    """
    if not _SLUG.match(slug) or len(slug) > SLUG_MAX:
        return JSONResponse(
            status_code=400,
            content={"error": "bad_slug", "detail": "lowercase letters, digits and hyphens"},
        )
    maintenant = _now()
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO place (slug, title, body, published_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (slug) DO UPDATE SET "
            "  title = excluded.title, body = excluded.body, updated_at = excluded.updated_at",
            (slug, payload.title, payload.body, maintenant, maintenant),
        )
    return {"slug": slug, "url": f"{config.BASE_URL}/place/{slug}", "updated_at": maintenant}


@router.delete("/place/{slug}", dependencies=[Depends(check_pc_token)])
async def retirer(slug: str):
    """Retire un document. Ce qui a été public l'a été : retirer n'efface pas l'ayant-lu."""
    with db.cursor() as cur:
        cur.execute("DELETE FROM place WHERE slug = ?", (slug,))
    return {"slug": slug, "removed": True}
