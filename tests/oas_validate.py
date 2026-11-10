"""Minimal JSON-Schema (OpenAPI 3.1 subset) validator for contract tests.

Covers what the vendored OpenAI component schemas use: type (incl. lists),
required, properties, items, enum, const, anyOf/oneOf/allOf, $ref to
#/components/schemas/*. No external dependency (jsonschema is not installed
in the NOCP venv). Schemas: tests/contracts/openai_subset.json, extracted from
ref/upstream/openai/openapi.yaml (openai/openai-openapi, MIT)."""
import json
from pathlib import Path

SCHEMAS = json.loads((Path(__file__).parent / "contracts" / "openai_subset.json")
                     .read_text())["schemas"]
_T = {"object": dict, "array": list, "string": str, "boolean": bool, "null": type(None)}


def _is(v, t):
    if t == "integer":
        return isinstance(v, int) and not isinstance(v, bool)
    if t == "number":
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    return isinstance(v, _T[t])


def errors(v, s, path="$"):
    if "$ref" in s:
        return errors(v, SCHEMAS[s["$ref"].split("/")[-1]], path)
    out = []
    if "allOf" in s:
        for x in s["allOf"]:
            out += errors(v, x, path)
    for k in ("anyOf", "oneOf"):
        if k in s and all(errors(v, x, path) for x in s[k]):
            out.append(f"{path}: matches no {k} branch")
    t = s.get("type")
    if t is not None:
        ts = t if isinstance(t, list) else [t]
        if not any(_is(v, x) for x in ts):
            return out + [f"{path}: {type(v).__name__} is not {t}"]
    if "enum" in s and v not in s["enum"]:
        out.append(f"{path}: {v!r} not in {s['enum']}")
    if "const" in s and v != s["const"]:
        out.append(f"{path}: {v!r} != {s['const']!r}")
    if isinstance(v, dict):
        for r in s.get("required", []):
            if r not in v:
                out.append(f"{path}: missing required {r}")
        for k, sub in s.get("properties", {}).items():
            if k in v:
                out += errors(v[k], sub, f"{path}.{k}")
    if isinstance(v, list) and "items" in s:
        for i, x in enumerate(v):
            out += errors(x, s["items"], f"{path}[{i}]")
    return out


def validate(v, name):
    errs = errors(v, SCHEMAS[name])
    assert not errs, f"{name}: " + "; ".join(errs[:10])
