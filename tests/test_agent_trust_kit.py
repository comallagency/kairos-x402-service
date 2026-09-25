from app.agent_trust_kit import VERSION, manifest


def test_manifest_version_and_formats():
    m = manifest()
    assert m["v"] == VERSION
    ids = {f["id"] for f in m["formats"]}
    assert "relationship-memory" in ids
    assert "tool-delivery-receipt" in ids
    assert "return-visit-pledge" in ids
    assert len(m["formats"]) >= 6


def test_manifest_workflow_buyer():
    m = manifest()
    buyer = m["workflow"]["buyer_x402"]
    assert buyer[0]["format"] == "relationship-memory"
    assert any(s.get("format") == "tool-delivery-receipt" for s in buyer)


def test_manifest_urls_absolute():
    m = manifest()
    for fmt in m["formats"]:
        assert fmt["schema"].startswith("http")
        assert fmt["validate"]["url"].startswith("http")
