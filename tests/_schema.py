"""Minimal JSON Schema check for contract tests (type/required/enum/pattern/
properties/items/minItems/minLength) — no jsonschema dependency."""
import re

_T = {"object": dict, "array": list, "string": str, "integer": int,
      "number": (int, float), "boolean": bool, "null": type(None)}


def check(inst, sch, path="$"):
    errs = []
    t = sch.get("type")
    if t:
        ts = t if isinstance(t, list) else [t]
        ok = any(isinstance(inst, _T[x]) and not (x in ("integer", "number")
                                                  and isinstance(inst, bool)) for x in ts)
        if not ok:
            return [f"{path}: expected {t}, got {type(inst).__name__}"]
    if "enum" in sch and inst not in sch["enum"]:
        errs.append(f"{path}: {inst!r} not in enum")
    if "pattern" in sch and isinstance(inst, str) and not re.search(sch["pattern"], inst):
        errs.append(f"{path}: pattern {sch['pattern']}")
    if isinstance(inst, str) and len(inst) < sch.get("minLength", 0):
        errs.append(f"{path}: minLength")
    if isinstance(inst, dict):
        errs += [f"{path}: missing {k}" for k in sch.get("required", []) if k not in inst]
        for k, s in sch.get("properties", {}).items():
            if k in inst:
                errs += check(inst[k], s, f"{path}.{k}")
    if isinstance(inst, list):
        if len(inst) < sch.get("minItems", 0):
            errs.append(f"{path}: minItems")
        if "items" in sch:
            for i, x in enumerate(inst):
                errs += check(x, sch["items"], f"{path}[{i}]")
    return errs
