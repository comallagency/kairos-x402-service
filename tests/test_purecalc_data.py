"""5 reference-verified cases per route. JSONPath results checked against
jsonpath-ng's own documented base syntax (verified interactively before
writing these - filter expressions like [?(...)] need the .ext submodule,
not used here). diff/flatten/schema-infer traced by hand against their own
simple, documented recursive rules."""

import pytest

from app.purecalc.registry import ComputeError
from app.purecalc.routes.data import (
    JsonDiffInput, JsonFlattenInput, JsonQueryInput, JsonValidateInput,
    RegexReplaceInput, RegexTestInput, SchemaInferInput,
    compute_json_diff, compute_json_flatten, compute_json_query,
    compute_json_validate, compute_regex_replace, compute_regex_test,
    compute_schema_infer,
)


# ------------------------------------------------------------------ json/query
def test_query_wildcard_array():
    r = compute_json_query(JsonQueryInput(
        data={"store": {"book": [{"title": "A"}, {"title": "B"}]}}, path="$.store.book[*].title",
    ))
    assert r.matches == ["A", "B"]


def test_query_index():
    r = compute_json_query(JsonQueryInput(
        data={"store": {"book": [{"title": "A"}, {"title": "B"}]}}, path="$.store.book[0].title",
    ))
    assert r.matches == ["A"]


def test_query_recursive_descent():
    r = compute_json_query(JsonQueryInput(
        data={"store": {"book": [{"price": 10}, {"price": 20}]}}, path="$..price",
    ))
    assert r.matches == [10, 20]


def test_query_no_match_empty_list():
    r = compute_json_query(JsonQueryInput(data={"a": 1}, path="$.nonexistent"))
    assert r.matches == []


def test_query_invalid_jsonpath_rejected():
    with pytest.raises(ComputeError):
        compute_json_query(JsonQueryInput(data={"a": 1}, path="not a jsonpath {{{"))


# ------------------------------------------------------------------- json/diff
def test_diff_changed_value():
    r = compute_json_diff(JsonDiffInput(a={"name": "Alice", "age": 30}, b={"name": "Alice", "age": 31}))
    assert len(r.changes) == 1
    assert r.changes[0].path == "$.age" and r.changes[0].change == "changed"
    assert r.changes[0].old == 30 and r.changes[0].new == 31


def test_diff_added_key():
    r = compute_json_diff(JsonDiffInput(a={"a": 1}, b={"a": 1, "b": 2}))
    assert len(r.changes) == 1
    assert r.changes[0].path == "$.b" and r.changes[0].change == "added"


def test_diff_removed_key():
    r = compute_json_diff(JsonDiffInput(a={"a": 1, "b": 2}, b={"a": 1}))
    assert len(r.changes) == 1
    assert r.changes[0].path == "$.b" and r.changes[0].change == "removed"


def test_diff_identical_no_changes():
    r = compute_json_diff(JsonDiffInput(a={"a": 1, "b": [1, 2]}, b={"a": 1, "b": [1, 2]}))
    assert r.changes == []


def test_diff_nested_list_element_changed():
    r = compute_json_diff(JsonDiffInput(a={"items": [1, 2, 3]}, b={"items": [1, 5, 3]}))
    assert len(r.changes) == 1
    assert r.changes[0].path == "$.items[1]"
    assert r.changes[0].old == 2 and r.changes[0].new == 5


# ---------------------------------------------------------------- json/flatten
def test_flatten_nested_dict_and_list():
    r = compute_json_flatten(JsonFlattenInput(data={"a": {"b": 1, "c": [2, 3]}}))
    assert r.flattened == {"a.b": 1, "a.c.0": 2, "a.c.1": 3}


def test_flatten_flat_dict_unchanged():
    r = compute_json_flatten(JsonFlattenInput(data={"x": 1, "y": 2}))
    assert r.flattened == {"x": 1, "y": 2}


def test_flatten_custom_separator():
    r = compute_json_flatten(JsonFlattenInput(data={"a": {"b": 1}}, separator="/"))
    assert r.flattened == {"a/b": 1}


def test_flatten_list_of_dicts():
    r = compute_json_flatten(JsonFlattenInput(data={"items": [{"id": 1}, {"id": 2}]}))
    assert r.flattened == {"items.0.id": 1, "items.1.id": 2}


def test_flatten_scalar_input():
    r = compute_json_flatten(JsonFlattenInput(data=42))
    assert r.flattened == {"": 42}


# ----------------------------------------------------------- json/schema-infer
def test_schema_infer_object_with_primitives():
    r = compute_schema_infer(SchemaInferInput(data={"name": "Alice", "age": 30, "active": True}))
    assert r.schema["type"] == "object"
    assert r.schema["properties"]["name"] == {"type": "string"}
    assert r.schema["properties"]["age"] == {"type": "integer"}
    assert r.schema["properties"]["active"] == {"type": "boolean"}


def test_schema_infer_array_of_strings():
    r = compute_schema_infer(SchemaInferInput(data=["a", "b", "c"]))
    assert r.schema == {"type": "array", "items": {"type": "string"}}


def test_schema_infer_empty_array():
    r = compute_schema_infer(SchemaInferInput(data=[]))
    assert r.schema == {"type": "array", "items": {}}


def test_schema_infer_null():
    r = compute_schema_infer(SchemaInferInput(data=None))
    assert r.schema == {"type": "null"}


def test_schema_infer_nested_object():
    r = compute_schema_infer(SchemaInferInput(data={"address": {"city": "Paris"}}))
    assert r.schema["properties"]["address"] == {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}


# ------------------------------------------------------------- json/validate
def test_validate_passes():
    r = compute_json_validate(JsonValidateInput(
        data={"name": "Alice"},
        schema={"type": "object", "required": ["name"], "properties": {"name": {"type": "string"}}},
    ))
    assert r.valid is True and r.errors == []


def test_validate_fails_missing_required():
    r = compute_json_validate(JsonValidateInput(
        data={}, schema={"type": "object", "required": ["name"]},
    ))
    assert r.valid is False and len(r.errors) == 1


def test_validate_fails_wrong_type():
    r = compute_json_validate(JsonValidateInput(
        data={"age": "not a number"}, schema={"type": "object", "properties": {"age": {"type": "integer"}}},
    ))
    assert r.valid is False


def test_validate_invalid_schema_rejected():
    with pytest.raises(ComputeError):
        compute_json_validate(JsonValidateInput(data={}, schema={"type": "not-a-real-type"}))


def test_validate_array_items_schema():
    r = compute_json_validate(JsonValidateInput(
        data=[1, 2, "three"], schema={"type": "array", "items": {"type": "integer"}},
    ))
    assert r.valid is False


# -------------------------------------------------------------------- regex/test
def test_regex_test_matches_with_groups():
    r = compute_regex_test(RegexTestInput(pattern=r"(\d{4})-(\d{2})-(\d{2})", text="Date: 2026-03-15"))
    assert r.matches is True
    assert r.groups == ["2026", "03", "15"]
    assert r.span == [6, 16]


def test_regex_test_no_match():
    r = compute_regex_test(RegexTestInput(pattern=r"\d+", text="no digits here"))
    assert r.matches is False and r.span is None


def test_regex_test_ignore_case():
    r = compute_regex_test(RegexTestInput(pattern="hello", text="HELLO world", ignore_case=True))
    assert r.matches is True


def test_regex_test_case_sensitive_by_default():
    r = compute_regex_test(RegexTestInput(pattern="hello", text="HELLO world"))
    assert r.matches is False


def test_regex_test_invalid_pattern_rejected():
    with pytest.raises(ComputeError):
        compute_regex_test(RegexTestInput(pattern="(unclosed", text="x"))


# ----------------------------------------------------------------- regex/replace
def test_replace_collapses_whitespace():
    r = compute_regex_replace(RegexReplaceInput(pattern=r"\s+", text="too   many    spaces", replacement=" "))
    assert r.result == "too many spaces" and r.replacements == 2


def test_replace_count_limit():
    r = compute_regex_replace(RegexReplaceInput(pattern="a", text="aaaa", replacement="b", count=2))
    assert r.result == "bbaa" and r.replacements == 2


def test_replace_no_match_unchanged():
    r = compute_regex_replace(RegexReplaceInput(pattern="xyz", text="hello", replacement="_"))
    assert r.result == "hello" and r.replacements == 0


def test_replace_capture_group_backreference():
    r = compute_regex_replace(RegexReplaceInput(pattern=r"(\w+)@(\w+)", text="user@host", replacement=r"\2@\1"))
    assert r.result == "host@user"


def test_replace_invalid_pattern_rejected():
    with pytest.raises(ComputeError):
        compute_regex_replace(RegexReplaceInput(pattern="(unclosed", text="x", replacement="y"))
