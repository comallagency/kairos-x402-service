"""GET /sitemap.xml et lien depuis robots.txt."""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_sitemap_lists_discover_and_accueil():
    r = client.get("/sitemap.xml")
    assert r.status_code == 200
    assert "application/xml" in r.headers.get("content-type", "") or r.text.startswith("<?xml")
    body = r.text
    assert "/discover" in body
    assert "/accueil" in body
    assert "/place/discover-exemple-post-payant" in body


def test_robots_points_at_sitemap():
    r = client.get("/robots.txt")
    assert r.status_code == 200
    assert "Sitemap:" in r.text
    assert "/sitemap.xml" in r.text
