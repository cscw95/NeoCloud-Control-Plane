#!/usr/bin/env python3
"""Generate `neocloud_sdk/_generated.py` from the NOCP OpenAPI document (F29).

    python packaging/sdk/gen.py                    # from packaging/openapi/nocp-openapi.json
    python packaging/sdk/gen.py --from-app         # re-export the snapshot from app.main first
    python packaging/sdk/gen.py --check            # exit 1 if _generated.py is stale

Naming (deterministic, like openapi-generator's tag grouping used by NICo
`rest-api/sdk/standard/`): one namespace per resource, one method per operation.
  namespace: /v1/*            → `openai`   (OpenAI-compatible token API)
             /realms/*        → `oidc`     (Keycloak-shaped OIDC issuer)
             /api/v1/<seg>/…  → `<seg>`    (hyphens → underscores)
             /<seg>/…         → `<seg>`
  method:    the FastAPI handler name (operationId minus its `_<path>_<method>` suffix);
             a clash inside one namespace gets the HTTP method appended.
"""

from __future__ import annotations

import argparse
import json
import keyword
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
SNAPSHOT = PKG / "openapi" / "nocp-openapi.json"
OUT = HERE / "neocloud_sdk" / "_generated.py"
METHODS = ("get", "post", "put", "patch", "delete")


def export_from_app() -> dict:
    root = PKG.parent
    sys.path.insert(0, str(root))
    import warnings
    warnings.simplefilter("ignore")
    from app.main import app  # noqa: E402
    spec = app.openapi()
    SNAPSHOT.write_text(json.dumps(spec, indent=1, sort_keys=True, ensure_ascii=False) + "\n")
    return spec


def namespace(path: str) -> str:
    segs = [s for s in path.split("/") if s]
    if segs[:1] == ["v1"]:
        ns = "openai"
    elif segs[:1] == ["realms"]:
        ns = "oidc"
    elif segs[:2] == ["api", "v1"] and len(segs) > 2:
        ns = segs[2]
    elif segs[:1] == ["admin"] and segs[1:2] == ["realms"]:
        ns = "oidc_admin"
    else:
        ns = segs[0] if segs else "root"
    ns = re.sub(r"\W", "_", ns.split(":")[0]).strip("_").lower() or "root"
    return "ns_" + ns if ns[0].isdigit() or keyword.iskeyword(ns) else ns


def handler_name(op_id: str, path: str, method: str) -> str:
    name = op_id
    for m in (method,) + METHODS:   # FastAPI may reuse one id across methods
        suffix = re.sub(r"\W", "_", path) + "_" + m
        if op_id.endswith(suffix):
            name = op_id[: -len(suffix)]
            break
    return pyname(name or method)


def pyname(s: str) -> str:
    s = re.sub(r"\W", "_", s).strip("_").lower() or "param"
    if s[0].isdigit():
        s = "p_" + s
    return s + "_" if keyword.iskeyword(s) or s in ("self", "body", "headers", "stream") else s


def body_kind(op: dict):
    rb = op.get("requestBody")
    if not rb:
        return None
    content = rb.get("content", {})
    if "application/x-www-form-urlencoded" in content or "multipart/form-data" in content:
        return "form"
    return "json"


def body_ref(op: dict):
    rb = op.get("requestBody") or {}
    for c in (rb.get("content") or {}).values():
        ref = (c.get("schema") or {}).get("$ref")
        if ref:
            return ref.rsplit("/", 1)[-1]
    return None


def collect(spec: dict) -> list[dict]:
    ops, seen = [], {}
    for path in sorted(spec["paths"]):
        item = spec["paths"][path]
        for method in METHODS:
            if method not in item:
                continue
            op = item[method]
            ns = namespace(path)
            name = handler_name(op.get("operationId", method), path, method)
            params = [p for p in op.get("parameters", []) + item.get("parameters", [])
                      if p.get("in") in ("path", "query")]
            row = {
                "id": "", "ns": ns, "name": name, "method": method.upper(), "path": path,
                "summary": op.get("summary", ""),
                "path_params": [{"name": p["name"], "py": pyname(p["name"])}
                                for p in params if p["in"] == "path"],
                "query_params": [{"name": p["name"], "py": pyname(p["name"]),
                                  "required": bool(p.get("required"))}
                                 for p in params if p["in"] == "query"],
                "body": body_kind(op), "body_schema": body_ref(op),
                "body_required": bool((op.get("requestBody") or {}).get("required")),
            }
            ops.append(row)
    # resolve clashes: same (ns, name) → append method, then a counter
    by = {}
    for r in ops:
        by.setdefault((r["ns"], r["name"]), []).append(r)
    for (ns, name), rows in by.items():
        if len(rows) > 1:
            for r in rows:
                r["name"] = f"{name}_{r['method'].lower()}"
    by2 = {}
    for r in ops:
        k = (r["ns"], r["name"])
        n = by2.get(k, 0)
        by2[k] = n + 1
        if n:
            r["name"] = f"{r['name']}_{n + 1}"
        r["id"] = f"{r['ns']}.{r['name']}"
    for r in ops:
        assert r["id"] not in seen, r["id"]
        seen[r["id"]] = r
    return ops


def class_name(ns: str) -> str:
    return "".join(p.capitalize() for p in ns.split("_")) + "API"


def render(spec: dict, ops: list[dict]) -> str:
    info = spec.get("info", {})
    out = [
        '"""GENERATED by packaging/sdk/gen.py from packaging/openapi/nocp-openapi.json — do not edit.',
        "",
        f"Source: {info.get('title', '')} {info.get('version', '')} (OpenAPI {spec.get('openapi', '')}),",
        f"{len(ops)} operations.",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        "from typing import Any, Optional",
        "",
        "from ._client import BaseClient",
        "",
        f"API_TITLE = {info.get('title', '')!r}",
        f"API_VERSION = {info.get('version', '')!r}",
        "",
        "OPERATIONS = {",
    ]
    for r in ops:
        row = {k: r[k] for k in ("id", "ns", "name", "method", "path", "summary", "path_params",
                                 "query_params", "body", "body_schema", "body_required")}
        out.append(f"    {r['id']!r}: {row!r},")
    out += ["}", "", ""]
    out += ["class _Namespace:", "    def __init__(self, client: BaseClient):",
            "        self._c = client", "", ""]
    namespaces = sorted({r["ns"] for r in ops})
    sys.path.insert(0, str(HERE))
    from neocloud_sdk._client import BaseClient
    probe = BaseClient("http://x")
    reserved = set(dir(probe)) | set(vars(probe))
    clash = [ns for ns in namespaces if ns in reserved]
    assert not clash, f"namespace clashes with BaseClient attributes: {clash}"
    for ns in namespaces:
        out.append(f"class {class_name(ns)}(_Namespace):")
        out.append(f'    """Operations under the `{ns}` resource."""')
        out.append("")
        for r in [x for x in ops if x["ns"] == ns]:
            args = ["self"] + [f"{p['py']}: Any" for p in r["path_params"]]
            if r["body"]:
                args.append("body: Optional[dict] = None")
            kw = [f"{q['py']}: Any = None" for q in r["query_params"]]
            kw += ["headers: Optional[dict] = None"]
            sig = ", ".join(args + ["*"] + kw)
            out.append(f"    def {r['name']}({sig}) -> Any:")
            doc = r["summary"] or r["name"]
            req = [q["name"] for q in r["query_params"] if q["required"]]
            extra = f" · body: {r['body_schema'] or r['body']}" if r["body"] else ""
            extra += f" · required query: {', '.join(req)}" if req else ""
            out.append(f'        """{doc}. `{r["method"]} {r["path"]}`{extra}"""')
            pp = ", ".join(f"{p['name']!r}: {p['py']}" for p in r["path_params"])
            qq = ", ".join(f"{q['name']!r}: {q['py']}" for q in r["query_params"])
            b = "body" if r["body"] else "None"
            out.append(f"        return self._c.call(OPERATIONS[{r['id']!r}], {{{pp}}}, {{{qq}}}, {b}, headers)")
            out.append("")
        out.append("")
    out.append("class GeneratedClient(BaseClient):")
    out.append('    """All namespaces as attributes: `client.contracts.create_contract(...)`."""')
    out.append("")
    out.append("    def __init__(self, *args, **kwargs):")
    out.append("        super().__init__(*args, **kwargs)")
    for ns in namespaces:
        out.append(f"        self.{ns} = {class_name(ns)}(self)")
    out.append("")
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--from-app", action="store_true", help="re-export the OpenAPI snapshot from app.main")
    ap.add_argument("--check", action="store_true", help="fail if _generated.py is out of date")
    a = ap.parse_args(argv)
    spec = export_from_app() if a.from_app else json.loads(SNAPSHOT.read_text())
    ops = collect(spec)
    text = render(spec, ops)
    if a.check:
        if not OUT.exists() or OUT.read_text() != text:
            print("stale: run packaging/sdk/gen.py", file=sys.stderr)
            return 1
        print(f"up to date ({len(ops)} operations)")
        return 0
    OUT.write_text(text)
    print(f"wrote {OUT.relative_to(PKG.parent)} ({len(ops)} operations, "
          f"{len({r['ns'] for r in ops})} namespaces)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
