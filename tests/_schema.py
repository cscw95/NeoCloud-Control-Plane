"""Tiny JSON Schema subset validator for contract tests (type, required,
properties, items, const, enum, pattern, minItems, minLength) — jsonschema is
not a dependency of this repo. `check` (F26/F27/F13 tests) and `errors`
(F07 tests) are the same function."""

import json
import re
from pathlib import Path

_DIR = Path(__file__).parent / "schemas"
_TYPES = {"object": dict, "array": list, "string": str, "integer": int,
          "number": (int, float), "boolean": bool, "null": type(None)}


def load(name: str) -> dict:
    return json.loads((_DIR / name).read_text())


def errors(inst, schema: dict, path: str = "$") -> list:
    out = []
    t = schema.get("type")
    if t:
        ts = t if isinstance(t, list) else [t]
        ok = any(isinstance(inst, _TYPES[x]) and not (x in ("integer", "number")
                                                      and isinstance(inst, bool))
                 for x in ts)
        if not ok:
            return [f"{path}: expected {t}, got {type(inst).__name__}"]
    if "const" in schema and inst != schema["const"]:
        out.append(f"{path}: expected const {schema['const']!r}")
    if "enum" in schema and inst not in schema["enum"]:
        out.append(f"{path}: {inst!r} not in enum")
    if "pattern" in schema and isinstance(inst, str) and not re.search(schema["pattern"], inst):
        out.append(f"{path}: {inst!r} !~ {schema['pattern']}")
    if isinstance(inst, str) and len(inst) < schema.get("minLength", 0):
        out.append(f"{path}: minLength")
    if isinstance(inst, dict):
        for k in schema.get("required", []):
            if k not in inst:
                out.append(f"{path}: missing {k}")
        for k, sub in schema.get("properties", {}).items():
            if k in inst:
                out += errors(inst[k], sub, f"{path}.{k}")
    if isinstance(inst, list):
        if len(inst) < schema.get("minItems", 0):
            out.append(f"{path}: minItems")
        if "items" in schema:
            for i, x in enumerate(inst):
                out += errors(x, schema["items"], f"{path}[{i}]")
    return out


check = errors


def assert_valid(inst, name: str) -> None:
    errs = errors(inst, load(name))
    assert not errs, errs
