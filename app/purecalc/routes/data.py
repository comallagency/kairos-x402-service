"""Data/JSON pure-compute routes. jsonschema (already a dependency, MIT
license) for json/validate. jsonpath-ng (new dependency, Apache 2.0, pure
Python, zero transitive deps - verified via `pip show` before adding) for
json/query, since JSONPath has no stdlib implementation and is error-prone
to hand-roll correctly. Everything else (diff/flatten/schema-infer,
regex/*) is stdlib only."""

import re
from typing import Any

import jsonschema
from functools import lru_cache

from jsonpath_ng import parse as _jsonpath_parse_uncached
from pydantic import BaseModel, Field

from app.purecalc.registry import ComputeError, ComputeSpec, register

JsonValue = Any


# ------------------------------------------------------------------ json/query
class JsonQueryInput(BaseModel):
    data: JsonValue
    path: str


class JsonQueryOutput(BaseModel):
    matches: list[JsonValue]


@lru_cache(maxsize=256)
def _parse_jsonpath_cached(path: str):
    # jsonpath_ng.parse() measured at ~10ms per call (vs <0.01ms for every
    # other route in this pack) - it's a real parser building an AST, not a
    # cheap regex compile. Most callers reuse a handful of distinct path
    # expressions, so caching the parsed expression (immutable, safe to
    # share) turns a per-request cost into a one-time one.
    return _jsonpath_parse_uncached(path)


def compute_json_query(inp: JsonQueryInput) -> JsonQueryOutput:
    try:
        expr = _parse_jsonpath_cached(inp.path)
    except Exception as exc:
        raise ComputeError("invalid_jsonpath", f"{inp.path!r} is not a valid JSONPath: {exc}") from exc
    matches = [m.value for m in expr.find(inp.data)]
    return JsonQueryOutput(matches=matches)


register(ComputeSpec(
    slug="json/query", price="$0.002", service_name="json-query",
    description="Query a JSON document with a JSONPath expression.",
    tags=["jsonpath", "json query", "data extraction", "json"],
    input_model=JsonQueryInput, output_model=JsonQueryOutput, compute=compute_json_query,
    sample_input={"data": {"store": {"book": [{"title": "A"}, {"title": "B"}]}}, "path": "$.store.book[*].title"},
    sample_output={"matches": ["A", "B"]},
))


# ------------------------------------------------------------------- json/diff
class JsonDiffInput(BaseModel):
    a: JsonValue
    b: JsonValue


class JsonDiffChange(BaseModel):
    path: str
    change: str
    old: JsonValue = None
    new: JsonValue = None


class JsonDiffOutput(BaseModel):
    changes: list[JsonDiffChange]


def _json_diff(a, b, path="$"):
    changes = []
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b), key=str):
            p = f"{path}.{k}"
            if k not in a:
                changes.append(JsonDiffChange(path=p, change="added", new=b[k]))
            elif k not in b:
                changes.append(JsonDiffChange(path=p, change="removed", old=a[k]))
            else:
                changes.extend(_json_diff(a[k], b[k], p))
    elif isinstance(a, list) and isinstance(b, list):
        for i in range(max(len(a), len(b))):
            p = f"{path}[{i}]"
            if i >= len(a):
                changes.append(JsonDiffChange(path=p, change="added", new=b[i]))
            elif i >= len(b):
                changes.append(JsonDiffChange(path=p, change="removed", old=a[i]))
            else:
                changes.extend(_json_diff(a[i], b[i], p))
    elif a != b:
        changes.append(JsonDiffChange(path=path, change="changed", old=a, new=b))
    return changes


def compute_json_diff(inp: JsonDiffInput) -> JsonDiffOutput:
    return JsonDiffOutput(changes=_json_diff(inp.a, inp.b))


register(ComputeSpec(
    slug="json/diff", price="$0.002", service_name="json-diff",
    description="Recursive diff between two JSON documents: added, removed and changed paths.",
    tags=["json diff", "compare json", "data comparison"],
    input_model=JsonDiffInput, output_model=JsonDiffOutput, compute=compute_json_diff,
    sample_input={"a": {"name": "Alice", "age": 30}, "b": {"name": "Alice", "age": 31}},
    sample_output={"changes": [{"path": "$.age", "change": "changed", "old": 30, "new": 31}]},
))


# ---------------------------------------------------------------- json/flatten
class JsonFlattenInput(BaseModel):
    data: JsonValue
    separator: str = "."


class JsonFlattenOutput(BaseModel):
    flattened: dict[str, JsonValue]


def _flatten(obj, sep, prefix=""):
    items = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = f"{prefix}{sep}{k}" if prefix else str(k)
            items.update(_flatten(v, sep, key))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            key = f"{prefix}{sep}{i}" if prefix else str(i)
            items.update(_flatten(v, sep, key))
    else:
        items[prefix] = obj
    return items


def compute_json_flatten(inp: JsonFlattenInput) -> JsonFlattenOutput:
    return JsonFlattenOutput(flattened=_flatten(inp.data, inp.separator))


register(ComputeSpec(
    slug="json/flatten", price="$0.002", service_name="json-flatten",
    description="Flatten a nested JSON document into single-level dotted-path keys.",
    tags=["json flatten", "nested json", "dot notation", "data transformation"],
    input_model=JsonFlattenInput, output_model=JsonFlattenOutput, compute=compute_json_flatten,
    sample_input={"data": {"a": {"b": 1, "c": [2, 3]}}},
    sample_output={"flattened": {"a.b": 1, "a.c.0": 2, "a.c.1": 3}},
))


# ----------------------------------------------------------- json/schema-infer
class SchemaInferInput(BaseModel):
    data: JsonValue


class SchemaInferOutput(BaseModel):
    schema: dict  # shadows BaseModel.schema() (Pydantic v1 relic) - harmless, cosmetic warning only


def _infer_type(v):
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, int):
        return "integer"
    if isinstance(v, float):
        return "number"
    if isinstance(v, str):
        return "string"
    if isinstance(v, list):
        return "array"
    if isinstance(v, dict):
        return "object"
    return "unknown"


def _infer_schema(v):
    t = _infer_type(v)
    if t == "object":
        return {
            "type": "object",
            "properties": {k: _infer_schema(val) for k, val in v.items()},
            "required": list(v.keys()),
        }
    if t == "array":
        if not v:
            return {"type": "array", "items": {}}
        return {"type": "array", "items": _infer_schema(v[0])}
    return {"type": t}


def compute_schema_infer(inp: SchemaInferInput) -> SchemaInferOutput:
    return SchemaInferOutput(schema=_infer_schema(inp.data))


register(ComputeSpec(
    slug="json/schema-infer", price="$0.002", service_name="json-schema-infer",
    description="Infer a basic JSON Schema (types, object properties, array item type) from an example document.",
    tags=["json schema", "schema inference", "data modeling"],
    input_model=SchemaInferInput, output_model=SchemaInferOutput, compute=compute_schema_infer,
    sample_input={"data": {"name": "Alice", "age": 30, "active": True}},
    sample_output={"schema": {"type": "object", "properties": {"name": {"type": "string"}, "age": {"type": "integer"}, "active": {"type": "boolean"}}, "required": ["name", "age", "active"]}},
))


# ------------------------------------------------------------- json/validate
class JsonValidateInput(BaseModel):
    data: JsonValue
    schema: dict  # shadows BaseModel.schema() (Pydantic v1 relic) - harmless, cosmetic warning only


class JsonValidateOutput(BaseModel):
    valid: bool
    errors: list[str]


def compute_json_validate(inp: JsonValidateInput) -> JsonValidateOutput:
    validator_cls = jsonschema.validators.validator_for(inp.schema)
    try:
        validator_cls.check_schema(inp.schema)
    except jsonschema.SchemaError as exc:
        raise ComputeError("invalid_schema", str(exc)[:300]) from exc
    validator = validator_cls(inp.schema)
    errors = [e.message for e in validator.iter_errors(inp.data)]
    return JsonValidateOutput(valid=not errors, errors=errors)


register(ComputeSpec(
    slug="json/validate", price="$0.002", service_name="json-validate",
    description="Validate a JSON document against a JSON Schema.",
    tags=["json schema validation", "json validate", "data validation"],
    input_model=JsonValidateInput, output_model=JsonValidateOutput, compute=compute_json_validate,
    sample_input={"data": {"name": "Alice"}, "schema": {"type": "object", "required": ["name"], "properties": {"name": {"type": "string"}}}},
    sample_output={"valid": True, "errors": []},
))


# ------------------------------------------------------------------- regex/test
class RegexTestInput(BaseModel):
    pattern: str
    text: str
    ignore_case: bool = False


class RegexTestOutput(BaseModel):
    matches: bool
    groups: list[str | None]
    span: list[int] | None


def compute_regex_test(inp: RegexTestInput) -> RegexTestOutput:
    try:
        flags = re.IGNORECASE if inp.ignore_case else 0
        m = re.search(inp.pattern, inp.text, flags)
    except re.error as exc:
        raise ComputeError("invalid_pattern", f"{inp.pattern!r} is not a valid regex: {exc}") from exc
    if m is None:
        return RegexTestOutput(matches=False, groups=[], span=None)
    return RegexTestOutput(matches=True, groups=list(m.groups()), span=[m.start(), m.end()])


register(ComputeSpec(
    slug="regex/test", price="$0.002", service_name="regex-test",
    description="Test a regular expression against text; returns match status, capture groups and span.",
    tags=["regex", "pattern matching", "regular expression", "text matching"],
    input_model=RegexTestInput, output_model=RegexTestOutput, compute=compute_regex_test,
    sample_input={"pattern": "(\\d{4})-(\\d{2})-(\\d{2})", "text": "Date: 2026-03-15"},
    sample_output={"matches": True, "groups": ["2026", "03", "15"], "span": [6, 16]},
))


# ---------------------------------------------------------------- regex/replace
class RegexReplaceInput(BaseModel):
    pattern: str
    text: str
    replacement: str
    count: int = 0


class RegexReplaceOutput(BaseModel):
    result: str
    replacements: int


def compute_regex_replace(inp: RegexReplaceInput) -> RegexReplaceOutput:
    try:
        compiled = re.compile(inp.pattern)
    except re.error as exc:
        raise ComputeError("invalid_pattern", f"{inp.pattern!r} is not a valid regex: {exc}") from exc
    result, n = compiled.subn(inp.replacement, inp.text, count=inp.count)
    return RegexReplaceOutput(result=result, replacements=n)


register(ComputeSpec(
    slug="regex/replace", price="$0.002", service_name="regex-replace",
    description="Replace regular expression matches in text, with an optional max-replacement count.",
    tags=["regex", "find and replace", "text substitution", "regular expression"],
    input_model=RegexReplaceInput, output_model=RegexReplaceOutput, compute=compute_regex_replace,
    sample_input={"pattern": "\\s+", "text": "too   many    spaces", "replacement": " "},
    sample_output={"result": "too many spaces", "replacements": 2},
))
