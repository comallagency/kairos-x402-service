from app.tool_delivery_receipt import SAMPLE_RECEIPT, validate_receipt


def test_sample_valid():
    normalized, errors, _ = validate_receipt(SAMPLE_RECEIPT)
    assert errors == []
    assert normalized is not None
    assert normalized["route"] == "/summarize"


def test_digest_verification_with_content():
    content = '{"url":"https://example.com","title":"Example","text":"Hello"}'
    receipt = {
        **SAMPLE_RECEIPT,
        "delivery": {
            **SAMPLE_RECEIPT["delivery"],
            "tool_name": "read_web_page",
            "digest": "1c3d51071ea068435eb28032563a81df18547b4bd2e067c1f0d3ba2e3e0b7161",
        },
    }
    _, errors, check = validate_receipt(receipt, content=content)
    assert not errors
    assert check is not None
    assert check["match"] is True


def test_bad_digest_format():
    bad = {**SAMPLE_RECEIPT, "delivery": {**SAMPLE_RECEIPT["delivery"], "digest": "nope"}}
    _, errors, _ = validate_receipt(bad)
    assert any(e["path"] == "delivery.digest" for e in errors)
