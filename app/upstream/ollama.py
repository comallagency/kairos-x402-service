import asyncio
import json
from typing import Any

import httpx

from app import config

# Gemma3:4b tourne en local sur le VPS (service systemd `ollama`, port 11434,
# host.docker.internal côté conteneur - voir docker-compose.yml) : ni clé, ni
# quota, ni dépendance au solde OpenRouter. Utilisé pour le jugement de
# /fact-check (voir app/handlers/fact_check.py), qui n'a besoin que d'un
# verdict JSON à partir de sources déjà collectées par SearXNG, pas d'un
# modèle de pointe.


class OllamaError(Exception):
    pass


async def chat_json(
    messages: list[dict[str, str]],
    max_tokens: int = 500,
    timeout: float = 60.0,
) -> dict[str, Any]:
    """Appelle /api/chat avec format=json (Ollama force alors une sortie JSON
    valide côté serveur, contrairement à OpenRouter où un modèle gratuit peut
    répondre du texte libre) et renvoie le contenu déjà parsé."""
    body = {
        "model": config.OLLAMA_MODEL,
        "messages": messages,
        "format": "json",
        "stream": False,
        "options": {"num_predict": max_tokens, "temperature": 0.2},
    }
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(f"{config.OLLAMA_URL}/api/chat", json=body)
    except httpx.TimeoutException:
        raise OllamaError("ollama_timeout")
    except httpx.RequestError as exc:
        raise OllamaError(f"ollama_failed: {exc}"[:200])

    if resp.status_code >= 400:
        raise OllamaError(f"Ollama error {resp.status_code}: {resp.text[:300]}")

    data = resp.json()
    try:
        content = data["message"]["content"]
    except (KeyError, TypeError) as exc:
        raise OllamaError(f"Ollama response missing message.content: {json.dumps(data)[:300]}") from exc
    if not content or not content.strip():
        raise OllamaError("Ollama returned empty content")

    try:
        return json.loads(content)
    except json.JSONDecodeError as exc:
        raise OllamaError(f"Ollama content is not valid JSON: {content[:300]}") from exc


async def embed(texts: list[str], timeout: float = 30.0) -> list[list[float]]:
    """Appelle /api/embeddings pour chaque texte avec nomic-embed-text (deja
    tire sur ce VPS, sans cle ni quota - voir /discover, app/handlers/discover.py).
    Un appel par texte : l'API Ollama /api/embeddings ne prend qu'une chaine a la fois."""
    vectors = []
    async with httpx.AsyncClient(timeout=timeout) as client:
        for text in texts:
            last_err: OllamaError | None = None
            for attempt in range(2):
                try:
                    resp = await client.post(
                        f"{config.OLLAMA_URL}/api/embeddings",
                        json={"model": "nomic-embed-text", "prompt": text},
                    )
                except httpx.TimeoutException:
                    last_err = OllamaError("ollama_embed_timeout")
                except httpx.RequestError as exc:
                    last_err = OllamaError(f"ollama_embed_failed: {exc}"[:200])
                else:
                    if resp.status_code >= 400:
                        last_err = OllamaError(
                            f"Ollama embed error {resp.status_code}: {resp.text[:300]}"
                        )
                    else:
                        data = resp.json()
                        embedding = data.get("embedding")
                        if not embedding:
                            last_err = OllamaError(
                                f"Ollama embed response missing 'embedding': {json.dumps(data)[:300]}"
                            )
                        else:
                            vectors.append(embedding)
                            last_err = None
                            break
                if attempt == 0:
                    await asyncio.sleep(0.35)
            if last_err is not None:
                raise last_err
    return vectors
