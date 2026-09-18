from app.agent_trust_kit import build_manifest


def test_manifest_lists_four_formats():
    m = build_manifest("https://example.test")
    assert m["v"] == 1
    assert len(m["formats"]) == 4
    ids = {f["id"] for f in m["formats"]}
    assert ids == {
        "relationship-memory",
        "tool-result-digest",
        "tool-delivery-receipt",
        "honest-delivery-refusal",
    }


def test_workflow_has_branches():
    m = build_manifest("https://example.test")
    journal = next(s for s in m["workflow"] if s["action"] == "journal_outcome")
    assert len(journal["branches"]) == 2
