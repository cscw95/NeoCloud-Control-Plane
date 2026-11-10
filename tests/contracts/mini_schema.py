"""Tiny JSON Schema subset validator (type, required, properties, enum,
anyOf/oneOf, not, $ref, minLength) — the NOCP venv has no jsonschema."""

from __future__ import annotations

_TYPES = {"object": dict, "array": list, "string": str, "boolean": bool,
          "null": type(None)}


def _type_ok(v, t) -> bool:
    if t == "integer":
        return isinstance(v, int) and not isinstance(v, bool)
    if t == "number":
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    return isinstance(v, _TYPES[t])


def errors(v, s: dict, root: dict, path: str = "$") -> list[str]:
    if "$ref" in s:
        ref = s["$ref"]
        if ref.startswith("#/components/schemas/"):
            return errors(v, root[ref.rsplit("/", 1)[1]], root, path)
        node = root
        for part in ref.lstrip("#/").split("/"):
            node = node[part]
        return errors(v, node, root, path)
    out = []
    t = s.get("type")
    if t is not None:
        ts = t if isinstance(t, list) else [t]
        if not any(_type_ok(v, x) for x in ts):
            return [f"{path}: expected {t}, got {type(v).__name__}"]
    if "enum" in s and v not in s["enum"]:
        out.append(f"{path}: {v!r} not in enum")
    if "minLength" in s and isinstance(v, str) and len(v) < s["minLength"]:
        out.append(f"{path}: shorter than {s['minLength']}")
    for key in ("anyOf", "oneOf"):
        if key in s and not any(not errors(v, sub, root, path) for sub in s[key]):
            out.append(f"{path}: no {key} branch matches")
    if "not" in s and not errors(v, s["not"], root, path):
        out.append(f"{path}: matches a `not` schema")
    if isinstance(v, dict):
        for r in s.get("required", []):
            if r not in v:
                out.append(f"{path}: missing {r}")
        for k, sub in (s.get("properties") or {}).items():
            if k in v:
                out += errors(v[k], sub, root, f"{path}.{k}")
    return out
