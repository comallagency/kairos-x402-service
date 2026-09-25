"""POST /accueil — porte d'entrée pour contact et inscription."""

import json

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_post_accueil_empty_body_returns_welcome_card() -> None:
    r = client.post("/accueil", headers={"Accept": "application/json"})
    assert r.status_code == 200
    data = r.json()
    assert data["who"]["name"] == "Kairos"
    assert data.get("post", {}).get("mode") == "welcome"
    assert "accueil_post" in data.get("how_to_talk", {})


def test_post_accueil_contact_roundtrip() -> None:
    r = client.post(
        "/accueil",
        json={
            "sender": "pytest-accueil-post/1.0",
            "subject": "hello via accueil",
            "body": "Registration probe from pytest.",
            "declares": {
                "what_i_do": "unit tests",
                "endpoint": "https://example.com/agent",
                "skills": ["pytest"],
            },
        },
    )
    assert r.status_code == 201
    data = r.json()
    assert data["via"] == "/accueil"
    assert data.get("poll")


def test_post_accueil_accepts_contact_sample_wrapper() -> None:
    sample = client.get("/contact/sample").json()
    wrapped = {"request": sample["request"]}
    r = client.post("/accueil", content=json.dumps(wrapped))
    assert r.status_code in (200, 201)
