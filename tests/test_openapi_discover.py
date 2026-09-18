"""Les indexeurs (x402watch, agentic-web) lisent l'OpenAPI pour hasInputExample."""

from app.main import inner_app


def test_post_discover_openapi_examples() -> None:
    schema = inner_app.openapi()
    post = schema["paths"]["/discover"]["post"]
    json_in = post["requestBody"]["content"]["application/json"]
    assert "example" in json_in
    assert json_in["example"]["q"]
    json_out = post["responses"]["200"]["content"]["application/json"]
    assert "example" in json_out
    assert json_out["example"]["results"]
