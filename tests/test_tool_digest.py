import json

from app.tool_digest import digest_tool_result, verify_tool_result_digest


def test_stable_digest():
    a = digest_tool_result("t", {"b": 2, "a": 1})
    b = digest_tool_result("t", '{"a":1,"b":2}')
    assert a["digest"] == b["digest"]


def test_tool_use_id_changes_digest():
    base = digest_tool_result("t", "x")
    bound = digest_tool_result("t", "x", tool_use_id="id1")
    assert base["digest"] != bound["digest"]


def test_verify():
    d = digest_tool_result("read_pdf", {"pages": 1}, tool_use_id="tu1")
    ok = verify_tool_result_digest(d["digest"], "read_pdf", {"pages": 1}, tool_use_id="tu1")
    assert ok["match"] is True
    bad = verify_tool_result_digest(d["digest"], "read_pdf", {"pages": 2}, tool_use_id="tu1")
    assert bad["match"] is False


if __name__ == "__main__":
    test_stable_digest()
    test_tool_use_id_changes_digest()
    test_verify()
    print("ok")
