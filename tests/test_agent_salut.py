"""GET /agent-salut — free signed greeting with a per-caller memory excerpt."""

import hashlib
import hmac
import json

from fastapi.testclient import TestClient

from app import config
from app.main import app

client = TestClient(app)


def _verify(payload: dict) -> bool:
    body = {k: v for k, v in payload.items() if k != "signature"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    expected = hmac.new(
        config.AGENT_SALUT_SECRET.encode(), canonical.encode(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, payload["signature"])


def test_agent_salut_first_visit_then_returning() -> None:
    name = "pytest-agent-salut-probe"
    first = client.get("/agent-salut", params={"name": name})
    assert first.status_code == 200
    data = first.json()
    assert data["agent"] == "Kairos"
    assert data["wallet"] == config.X402_PAY_TO
    assert _verify(data)

    second = client.get("/agent-salut", params={"name": name})
    data2 = second.json()
    assert data2["memory"]["returning"] is True
    assert data2["memory"]["calls"] >= 2
    assert _verify(data2)


def test_agent_salut_sample_has_documented_shape() -> None:
    r = client.get("/agent-salut/sample")
    assert r.status_code == 200
    data = r.json()
    assert data["agent"] == "Kairos"
    assert "verify" in data and data["verify"]["algorithm"] == "HMAC-SHA256"
