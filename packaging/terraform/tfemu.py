#!/usr/bin/env python3
"""tfemu — a Terraform CLI stand-in for the emulated neocloud provider (F29).

    tfemu.py -chdir=examples/us02-reserved-contract init|validate|plan|apply|destroy|show|output
    tfemu.py providers-schema            # same document as `terraform providers schema -json`
    tfemu.py hcl -chdir=DIR              # render main.tf.json as HCL (keeps main.tf in sync)

Configuration is Terraform's JSON syntax (`*.tf.json`, developer.hashicorp.com/terraform/
language/syntax/json): `terraform.required_providers`, `provider`, `variable`, `resource`,
`output`, with `${type.name.attr}` / `${var.x}` interpolation. Outputs follow Terraform:
  - state: `terraform.tfstate` in state format v4 (version, terraform_version, serial,
    lineage, outputs, resources[].instances[].attributes / dependencies),
  - `plan -json`/`show -json`: the plan representation (format_version 1.2,
    resource_changes[].change.actions = ["create"] | ["update"] | ["delete","create"] | ["delete"]),
  - `-auto-approve` is implied (no interactive prompt), `-var name=value` is supported.
Only the subset NeoCloud examples use is implemented; this is not a general Terraform.
"""

from __future__ import annotations

import json
import os
import re
import sys
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import provider as P  # noqa: E402
from neocloud_sdk import Client, NeoCloudError  # noqa: E402

TF_VERSION = "1.9.8"
REF = re.compile(r"\$\{([^}]+)\}")
PROVIDER_REF = f'provider["{P.PROVIDER_ADDR}"]'
UNKNOWN = object()


class TFError(Exception):
    pass


# --------------------------------------------------------------------------- config
def load_config(d: Path) -> dict:
    cfg = {"variable": {}, "resource": {}, "output": {}, "provider": {}, "terraform": {}}
    files = sorted(d.glob("*.tf.json"))
    if not files:
        raise TFError(f"No configuration files in {d} (expected *.tf.json)")
    for f in files:
        doc = json.loads(f.read_text())
        for k, v in doc.items():
            if k == "resource":
                for t, items in v.items():
                    cfg["resource"].setdefault(t, {}).update(items)
            elif k in cfg:
                cfg[k].update(v) if isinstance(v, dict) else None
    return cfg


def addresses(cfg: dict) -> dict:
    return {f"{t}.{n}": (t, n, body) for t, items in cfg["resource"].items() for n, body in items.items()}


def refs_in(v) -> set:
    out = set()
    if isinstance(v, str):
        for m in REF.findall(v):
            parts = m.strip().split(".")
            if parts[0] != "var" and len(parts) >= 2:
                out.add(".".join(parts[:2]))
    elif isinstance(v, dict):
        for x in v.values():
            out |= refs_in(x)
    elif isinstance(v, list):
        for x in v:
            out |= refs_in(x)
    return out


def order(cfg: dict) -> list[str]:
    addrs = addresses(cfg)
    deps = {a: {r for r in refs_in(b) if r in addrs} | set(b.get("depends_on", []))
            for a, (_, _, b) in addrs.items()}
    out, seen, stack = [], set(), set()

    def visit(a):
        if a in seen:
            return
        if a in stack:
            raise TFError(f"Cycle: {a}")
        stack.add(a)
        for d in sorted(deps[a]):
            if d not in addrs:
                raise TFError(f'Reference to undeclared resource "{d}"')
            visit(d)
        stack.discard(a)
        seen.add(a)
        out.append(a)

    for a in sorted(addrs):
        visit(a)
    return out


def resolve(v, variables: dict, values: dict):
    """Interpolate `${...}`; returns UNKNOWN when a value is only known after apply."""
    if isinstance(v, str):
        whole = REF.fullmatch(v.strip())
        if whole:
            return lookup(whole.group(1).strip(), variables, values)
        def sub(m):
            x = lookup(m.group(1).strip(), variables, values)
            if x is UNKNOWN:
                raise _Unknown()
            return str(x).lower() if isinstance(x, bool) else str(x)
        try:
            return REF.sub(sub, v)
        except _Unknown:
            return UNKNOWN
    if isinstance(v, dict):
        return {k: resolve(x, variables, values) for k, x in v.items()}
    if isinstance(v, list):
        return [resolve(x, variables, values) for x in v]
    return v


class _Unknown(Exception):
    pass


def lookup(expr: str, variables: dict, values: dict):
    parts = expr.split(".")
    if parts[0] == "var":
        if parts[1] not in variables:
            raise TFError(f'Reference to undeclared input variable "{parts[1]}"')
        return variables[parts[1]]
    addr = ".".join(parts[:2])
    if addr not in values:
        raise TFError(f'Reference to undeclared resource "{addr}"')
    obj = values[addr]
    if obj is UNKNOWN:
        return UNKNOWN
    for p in parts[2:]:
        if isinstance(obj, list):
            obj = obj[int(p)]
        else:
            obj = obj.get(p, UNKNOWN) if isinstance(obj, dict) else UNKNOWN
        if obj is UNKNOWN or obj is None:
            return UNKNOWN if obj is UNKNOWN else None
    return obj


def has_unknown(v) -> bool:
    if v is UNKNOWN:
        return True
    if isinstance(v, dict):
        return any(has_unknown(x) for x in v.values())
    if isinstance(v, list):
        return any(has_unknown(x) for x in v)
    return False


def variables_of(cfg: dict, cli_vars: dict) -> dict:
    out = {}
    for name, spec in cfg["variable"].items():
        env = os.environ.get(f"TF_VAR_{name}")
        if name in cli_vars:
            raw = cli_vars[name]
        elif env is not None:
            raw = env
        elif "default" in (spec or {}):
            out[name] = spec["default"]
            continue
        else:
            raise TFError(f'No value for required variable "{name}"')
        t = (spec or {}).get("type", "string")
        out[name] = json.loads(raw) if t in ("number", "bool") or t.startswith(("list", "map", "object")) else raw
    return out


# --------------------------------------------------------------------------- state
def load_state(d: Path) -> dict:
    f = d / "terraform.tfstate"
    if f.exists():
        return json.loads(f.read_text())
    return {"version": 4, "terraform_version": TF_VERSION, "serial": 0,
            "lineage": str(uuid.uuid4()), "outputs": {}, "resources": [], "check_results": None}


def state_index(st: dict) -> dict:
    return {f"{r['type']}.{r['name']}": r for r in st["resources"] if r["mode"] == "managed"}


def save_state(d: Path, st: dict) -> None:
    st["serial"] += 1
    st["terraform_version"] = TF_VERSION
    (d / "terraform.tfstate").write_text(json.dumps(st, indent=2, ensure_ascii=False) + "\n")


def make_client(cfg: dict, variables: dict) -> Client:
    pc = resolve((cfg["provider"].get("neocloud") or {}), variables, {})
    return Client(pc.get("base_url") or None, token=pc.get("token") or None,
                  operator=pc.get("operator"))


# --------------------------------------------------------------------------- plan
def plan(cfg: dict, st: dict, variables: dict, destroy: bool = False) -> list[dict]:
    idx = state_index(st)
    addrs = addresses(cfg)
    values = {a: idx[a]["instances"][0]["attributes"] for a in idx}
    changes = []
    if not destroy:
        for addr in order(cfg):
            rtype, name, body = addrs[addr]
            errs = P.validate(rtype, {k: v for k, v in body.items() if k != "depends_on"})
            if errs:
                raise TFError("\n".join(errs))
            want = P.defaults(rtype, resolve({k: v for k, v in body.items() if k != "depends_on"},
                                             variables, values))
            attrs = P.RESOURCES[rtype]["attributes"]
            cur = values.get(addr) if addr in idx else None
            if cur is None:
                actions = ["create"]
            else:
                diff = [k for k, v in want.items()
                        if not (attrs[k]["computed"] and v is None) and (v is UNKNOWN or cur.get(k) != v)]
                if not diff:
                    actions = ["no-op"]
                elif any(attrs[k]["force_new"] for k in diff):
                    actions = ["delete", "create"]
                elif "update" in P.RESOURCES[rtype]:
                    actions = ["update"]
                else:
                    actions = ["delete", "create"]
            keep = cur if actions in (["update"], ["no-op"]) else {}
            after = {**(keep or {}), **want}
            unknown = {k: True for k, a in attrs.items()
                       if (a["computed"] and actions[-1] == "create" and want.get(k) is None)
                       or after.get(k) is UNKNOWN}
            values[addr] = UNKNOWN if actions[-1] == "create" else {**(cur or {}), **want}
            changes.append({"address": addr, "mode": "managed", "type": rtype, "name": name,
                            "provider_name": P.PROVIDER_ADDR,
                            "change": {"actions": actions, "before": cur,
                                       "after": {k: (None if v is UNKNOWN else v) for k, v in after.items()},
                                       "after_unknown": unknown,
                                       "before_sensitive": _sens(rtype), "after_sensitive": _sens(rtype)}})
    gone = [a for a in idx if destroy or a not in addrs]
    deps = {a: set(idx[a]["instances"][0].get("dependencies", [])) for a in gone}
    for addr in _reverse_topo(gone, deps):
        r = idx[addr]
        changes.append({"address": addr, "mode": "managed", "type": r["type"], "name": r["name"],
                        "provider_name": P.PROVIDER_ADDR,
                        "change": {"actions": ["delete"], "before": r["instances"][0]["attributes"],
                                   "after": None, "after_unknown": {},
                                   "before_sensitive": _sens(r["type"]), "after_sensitive": False}})
    return changes


def _sens(rtype: str) -> dict:
    return {k: True for k, a in P.RESOURCES.get(rtype, {}).get("attributes", {}).items() if a["sensitive"]}


def _reverse_topo(addrs: list, deps: dict) -> list:
    out, seen = [], set()

    def visit(a):
        if a in seen:
            return
        seen.add(a)
        for d in sorted(deps.get(a, ())):
            if d in deps:
                visit(d)
        out.append(a)
    for a in sorted(addrs):
        visit(a)
    return list(reversed(out))


def plan_json(changes: list, cfg: dict) -> dict:
    return {"format_version": "1.2", "terraform_version": TF_VERSION,
            "resource_changes": changes, "errored": False,
            "configuration": {"provider_config": {"neocloud": {"name": "neocloud",
                                                               "full_name": P.PROVIDER_ADDR}}}}


def summary(changes: list) -> str:
    add = sum(1 for c in changes if c["change"]["actions"][-1] == "create")
    chg = sum(1 for c in changes if c["change"]["actions"] == ["update"])
    dst = sum(1 for c in changes if "delete" in c["change"]["actions"])
    return f"Plan: {add} to add, {chg} to change, {dst} to destroy."


def show_plan(changes: list) -> None:
    sym = {"create": "+", "update": "~", "delete": "-", "no-op": " "}
    for c in changes:
        a = c["change"]["actions"]
        if a == ["no-op"]:
            continue
        mark = "-/+" if a == ["delete", "create"] else sym[a[0]]
        print(f"  {mark} {c['address']}  ({' then '.join(a)})")
        after = c["change"]["after"] or {}
        for k in sorted(set(after) | set(c["change"]["after_unknown"])):
            if c["change"]["after_unknown"].get(k):
                v = "(known after apply)"
            elif c["change"]["after_sensitive"] and c["change"]["after_sensitive"].get(k):
                v = "(sensitive value)"
            else:
                v = json.dumps(after.get(k), ensure_ascii=False)
            if after.get(k) is not None or c["change"]["after_unknown"].get(k):
                print(f"        {k:16s} = {v}")
    print(summary(changes))


# --------------------------------------------------------------------------- apply
def apply(d: Path, cfg: dict, st: dict, variables: dict, destroy: bool = False, quiet: bool = False,
          client: Client | None = None) -> dict:
    client = client or make_client(cfg, variables)
    changes = plan(cfg, st, variables, destroy)
    idx = state_index(st)
    addrs = addresses(cfg)
    values = {a: idx[a]["instances"][0]["attributes"] for a in idx}
    log = []
    for ch in changes:
        addr, rtype, acts = ch["address"], ch["type"], ch["change"]["actions"]
        impl = P.RESOURCES[rtype]
        if "delete" in acts and addr in idx:
            note = impl["delete"](client, idx[addr]["instances"][0]["attributes"])
            st["resources"] = [r for r in st["resources"] if f"{r['type']}.{r['name']}" != addr]
            values.pop(addr, None)
            log.append(f"{addr}: Destruction complete ({note})")
            save_state(d, st)
        if acts[-1] in ("create", "update"):
            body = {k: v for k, v in addrs[addr][2].items() if k != "depends_on"}
            want = P.defaults(rtype, resolve(body, variables, values))
            if has_unknown(want):
                raise TFError(f"{addr}: value still unknown at apply time")
            if acts[-1] == "update":
                attrs = impl["update"](client, idx[addr]["instances"][0]["attributes"], want)
                verb = "Modifications complete"
            else:
                attrs = impl["create"](client, want)
                verb = "Creation complete"
            values[addr] = attrs
            deps = sorted(r for r in refs_in(body) if r in addrs)
            row = {"mode": "managed", "type": rtype, "name": ch["name"], "provider": PROVIDER_REF,
                   "instances": [{"schema_version": 0, "attributes": attrs,
                                  "sensitive_attributes": [[{"type": "get_attr", "value": k}]
                                                           for k in _sens(rtype)],
                                  "dependencies": deps}]}
            st["resources"] = [r for r in st["resources"] if f"{r['type']}.{r['name']}" != addr] + [row]
            log.append(f"{addr}: {verb} after 0s [id={attrs.get('id')}]")
            save_state(d, st)
    st["outputs"] = {} if destroy else outputs(cfg, variables, values)
    save_state(d, st)
    if not quiet:
        for line in log:
            print(line)
        add = sum(1 for c in changes if c["change"]["actions"][-1] == "create")
        chg = sum(1 for c in changes if c["change"]["actions"] == ["update"])
        dst = sum(1 for c in changes if "delete" in c["change"]["actions"])
        word = "Destroy" if destroy else "Apply"
        print(f"\n{word} complete! Resources: {add} added, {chg} changed, {dst} destroyed.")
        if st["outputs"]:
            print("\nOutputs:\n")
            for k, o in st["outputs"].items():
                print(f"{k} = {'<sensitive>' if o['sensitive'] else json.dumps(o['value'], ensure_ascii=False)}")
    return {"changes": changes, "log": log, "state": st}


def outputs(cfg: dict, variables: dict, values: dict) -> dict:
    out = {}
    for name, spec in cfg["output"].items():
        v = resolve(spec.get("value"), variables, values)
        t = "number" if isinstance(v, (int, float)) and not isinstance(v, bool) else \
            "bool" if isinstance(v, bool) else ["list", "string"] if isinstance(v, list) else "string"
        out[name] = {"value": v, "type": t, "sensitive": bool(spec.get("sensitive"))}
    return out


# --------------------------------------------------------------------------- HCL
def _hcl_val(v, ind: str) -> str:
    if isinstance(v, bool):
        return str(v).lower()
    if isinstance(v, (int, float)):
        return json.dumps(v)
    if isinstance(v, str):
        whole = REF.fullmatch(v)
        return whole.group(1) if whole else json.dumps(v, ensure_ascii=False)
    if isinstance(v, list):
        return "[" + ", ".join(_hcl_val(x, ind) for x in v) + "]"
    if isinstance(v, dict):
        inner = "\n".join(f"{ind}  {k} = {_hcl_val(x, ind + '  ')}" for k, x in v.items())
        return "{\n" + inner + f"\n{ind}}}"
    return "null"


def _hcl_body(body: dict, ind: str, block_keys=("nodepool", "required_providers")) -> list[str]:
    lines = []
    w = max((len(k) for k, v in body.items() if not isinstance(v, (list, dict))), default=0)
    for k, v in body.items():
        if k in block_keys and isinstance(v, list):
            for item in v:
                lines.append(f"{ind}{k} {{")
                lines += _hcl_body(item, ind + "  ")
                lines.append(f"{ind}}}")
        elif k in block_keys and isinstance(v, dict):
            lines.append(f"{ind}{k} {{")
            lines += _hcl_body(v, ind + "  ")
            lines.append(f"{ind}}}")
        elif k == "type" and isinstance(v, str):        # variable type constraint is a bare keyword
            lines.append(f"{ind}{k.ljust(w)} = {v}")
        elif k == "depends_on":
            lines.append(f"{ind}depends_on = [{', '.join(v)}]")
        else:
            lines.append(f"{ind}{k.ljust(w)} = {_hcl_val(v, ind)}")
    return lines


def to_hcl(d: Path) -> str:
    out = []
    for f in sorted(d.glob("*.tf.json")):
        doc = json.loads(f.read_text())
        if "//" in doc:
            out += [f"# {line}" for line in doc["//"].splitlines()] + [""]
        if "terraform" in doc:
            out += ["terraform {"] + _hcl_body(doc["terraform"], "  ") + ["}", ""]
        for name, spec in doc.get("variable", {}).items():
            out += [f'variable "{name}" {{'] + _hcl_body(spec, "  ") + ["}", ""]
        for name, spec in doc.get("provider", {}).items():
            out += [f'provider "{name}" {{'] + _hcl_body(spec, "  ") + ["}", ""]
        for t, items in doc.get("resource", {}).items():
            for n, body in items.items():
                out += [f'resource "{t}" "{n}" {{'] + _hcl_body(body, "  ") + ["}", ""]
        for name, spec in doc.get("output", {}).items():
            out += [f'output "{name}" {{'] + _hcl_body(spec, "  ") + ["}", ""]
    return "\n".join(out).rstrip() + "\n"


# --------------------------------------------------------------------------- CLI
def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    d, cli_vars, as_json = Path.cwd(), {}, False
    rest = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a.startswith("-chdir="):
            d = Path(a.split("=", 1)[1]).resolve()
        elif a == "-var":
            k, _, v = argv[i + 1].partition("=")
            cli_vars[k] = v
            i += 1
        elif a.startswith("-var="):
            k, _, v = a[5:].partition("=")
            cli_vars[k] = v
        elif a == "-json":
            as_json = True
        elif a in ("-auto-approve", "-input=false", "-no-color"):
            pass
        else:
            rest.append(a)
        i += 1
    cmd = rest[0] if rest else "help"
    try:
        if cmd == "providers-schema":
            print(json.dumps(P.providers_schema(), indent=2))
            return 0
        if cmd == "hcl":
            sys.stdout.write(to_hcl(d))
            return 0
        if cmd == "help":
            print(__doc__)
            return 0
        cfg = load_config(d)
        if cmd == "init":
            req = (cfg["terraform"].get("required_providers") or {}).get("neocloud") or {}
            src = req.get("source", "")
            if src not in ("neocloud/neocloud", P.PROVIDER_ADDR):
                raise TFError(f'required_providers.neocloud.source must be "neocloud/neocloud", got "{src}"')
            print(f"- Installing neocloud/neocloud v{P.PROVIDER_VERSION} (emulated, in-process)...")
            print("Terraform has been successfully initialized!")
            return 0
        st = load_state(d)
        variables = variables_of(cfg, cli_vars)
        if cmd == "validate":
            order(cfg)
            for addr, (t, _, body) in addresses(cfg).items():
                errs = P.validate(t, {k: v for k, v in body.items() if k != "depends_on"})
                if errs:
                    raise TFError("\n".join(errs))
            print(json.dumps({"valid": True, "error_count": 0}) if as_json
                  else "Success! The configuration is valid.")
            return 0
        if cmd in ("plan", "apply", "destroy"):
            destroy = cmd == "destroy" or "-destroy" in rest
            if cmd == "plan":
                ch = plan(cfg, st, variables, destroy)
                if as_json:
                    print(json.dumps(plan_json(ch, cfg), indent=2, ensure_ascii=False))
                else:
                    show_plan(ch)
                return 0
            apply(d, cfg, st, variables, destroy)
            return 0
        if cmd == "show":
            print(json.dumps(st, indent=2, ensure_ascii=False) if as_json else
                  "\n".join(f"{r['type']}.{r['name']}  id={r['instances'][0]['attributes'].get('id')}"
                            for r in st["resources"]) or "The state file is empty.")
            return 0
        if cmd == "output":
            outs = st.get("outputs", {})
            if len(rest) > 1:
                o = outs.get(rest[1])
                if o is None:
                    raise TFError(f'Output "{rest[1]}" not found')
                v = o["value"]
                print(json.dumps(v) if as_json or not isinstance(v, (str, int, float)) else v)
            else:
                print(json.dumps({k: v for k, v in outs.items()}, indent=2) if as_json else
                      "\n".join(f"{k} = {'<sensitive>' if v['sensitive'] else json.dumps(v['value'])}"
                                for k, v in outs.items()))
            return 0
        raise TFError(f"unknown command {cmd}")
    except (TFError, NeoCloudError, RuntimeError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
