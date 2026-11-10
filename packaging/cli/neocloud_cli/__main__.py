"""`neocloud` CLI (F29): every NOCP operation as `neocloud <namespace> <operation>`.

Commands are built at start-up from the generated SDK table (neocloud_sdk.OPERATIONS),
so the CLI never drifts from the OpenAPI document. Body fields follow `gh api`:
  -f key=value   string field          -F key=value   typed field (JSON: 4, true, {"a":1})
  dotted keys nest: -F placement.same_ib_zone=true;  --body '{"…"}' or --body @file.json
Examples:
  neocloud --operator contracts create-contract -f tenant_id=tnt-a -F racks=4 -F term_months=12 ...
  neocloud --operator orders create-order -f tenant_id=tnt-a -f kind=new -f contract_id=ctr-1 -q state
  neocloud --api-key "$KEY" chat -m llama-3.3-70b-instruct --stream "hello"
  neocloud commands --ns nvke
Global options also read NEOCLOUD_BASE_URL / NEOCLOUD_TOKEN / NEOCLOUD_API_KEY /
NEOCLOUD_OPERATOR=1 / NEOCLOUD_TENANT_ID. Exit codes: 0 ok, 1 API error, 2 usage,
3 rate limited (429; Retry-After printed to stderr), 4 connection error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

try:
    import neocloud_sdk  # noqa: F401
except ImportError:  # running from a source checkout
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "sdk"))

from neocloud_sdk import (OPERATIONS, APIConnectionError, APIStatusError, Client,  # noqa: E402
                          RateLimitError)

PROG = "neocloud"


def _cmd(name: str) -> str:
    return name.replace("_", "-")


def _set(d: dict, dotted: str, value) -> None:
    keys = dotted.split(".")
    for k in keys[:-1]:
        d = d.setdefault(k, {})
    d[keys[-1]] = value


def _typed(raw: str):
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def build_body(args) -> dict | None:
    body = None
    if args.body:
        text = Path(args.body[1:]).read_text() if args.body.startswith("@") else args.body
        body = json.loads(text)
    for kind, items in (("f", args.field or []), ("F", args.raw_field or [])):
        for item in items:
            if "=" not in item:
                raise SystemExit(f"{PROG}: -{kind} expects key=value, got '{item}'")
            k, _, v = item.partition("=")
            body = {} if body is None else body
            _set(body, k, v if kind == "f" else _typed(v))
    return body


def pick(obj, path: str):
    for part in [p for p in path.split(".") if p]:
        if isinstance(obj, list):
            if part == "length":
                obj = len(obj)
                continue
            obj = obj[int(part)]
        elif isinstance(obj, dict):
            obj = obj.get(part)
        else:
            return None
    return obj


def emit(obj, query: str | None) -> None:
    if query:
        obj = pick(obj, query)
        if isinstance(obj, (str, int, float)) and not isinstance(obj, bool):
            print(obj)
            return
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog=PROG, description="NeoCloud CLI (generated from the NOCP OpenAPI)",
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__.split("\n", 1)[1])
    ap.add_argument("--base-url", default=os.environ.get("NEOCLOUD_BASE_URL"))
    ap.add_argument("--token", default=None, help="Bearer token (NEOCLOUD_TOKEN)")
    ap.add_argument("--api-key", default=None, help="token-API key for /v1/* (NEOCLOUD_API_KEY)")
    ap.add_argument("--operator", action="store_true", default=None,
                    help="send X-Operator: true (dev stacks with NOCP_DEV_INSECURE_HEADERS=1)")
    ap.add_argument("--tenant", default=None, help="send X-Tenant-Id (dev stacks)")
    ap.add_argument("-q", "--query", default=None, help="print one field, dotted path (e.g. id, node_ids.length)")
    ap.add_argument("-v", "--verbose", action="store_true", help="print the request line to stderr")
    sub = ap.add_subparsers(dest="ns", metavar="<namespace>")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-q", "--query", default=argparse.SUPPRESS,
                        help="print one field, dotted path (e.g. id, node_ids.length)")
    common.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS)
    ops = sub.add_parser("commands", help="list every generated operation")
    ops.add_argument("--ns", dest="ops_ns", default=None, help="only this namespace")

    chat = sub.add_parser("chat", parents=[common], help="OpenAI-compatible chat completion (POST /v1/chat/completions)")
    chat.add_argument("prompt")
    chat.add_argument("-m", "--model", default=None, help="default: first of GET /v1/models")
    chat.add_argument("--system", default=None)
    chat.add_argument("--max-tokens", type=int, default=64)
    chat.add_argument("--stream", action="store_true")
    chat.add_argument("--request-id", default=None, help="x-request-id header (idempotency)")

    by_ns: dict[str, list] = {}
    for op in OPERATIONS.values():
        by_ns.setdefault(op["ns"], []).append(op)
    for ns in sorted(by_ns):
        p = sub.add_parser(_cmd(ns), help=f"{len(by_ns[ns])} operations")
        osub = p.add_subparsers(dest="op", metavar="<operation>")
        for op in sorted(by_ns[ns], key=lambda o: o["name"]):
            o = osub.add_parser(_cmd(op["name"]), parents=[common], help=f"{op['method']} {op['path']}",
                                description=f"{op['summary']}\n{op['method']} {op['path']}")
            for pp in op["path_params"]:
                o.add_argument(pp["py"], metavar=pp["name"].upper())
            for q in op["query_params"]:
                o.add_argument("--" + q["name"].replace("_", "-"), dest="q_" + q["py"],
                               required=q["required"], default=None)
            if op["body"]:
                o.add_argument("-f", "--field", action="append", help="string body field key=value")
                o.add_argument("-F", "--raw-field", action="append", help="typed body field key=json")
                o.add_argument("--body", default=None, help="JSON body or @file.json")
            o.set_defaults(_op=op)
    return ap


def client_from(args) -> Client:
    return Client(args.base_url, token=args.token, api_key=args.api_key,
                  operator=args.operator, tenant_id=args.tenant)


def run_chat(c: Client, args) -> int:
    o = c.openai_client()
    model = args.model or o.models.list().data[0].id
    msgs = ([{"role": "system", "content": args.system}] if args.system else []) + \
        [{"role": "user", "content": args.prompt}]
    hdr = {"x-request-id": args.request_id} if args.request_id else None
    if not args.stream:
        r = o.chat.completions.create(model=model, messages=msgs, max_tokens=args.max_tokens,
                                      extra_headers=hdr)
        if args.query:
            emit(r, args.query)
        else:
            print(r.choices[0].message.content)
            print(json.dumps({"usage": r.get("usage")}, ensure_ascii=False), file=sys.stderr)
        return 0
    usage, n = None, 0
    for chunk in o.chat.completions.create(model=model, messages=msgs, max_tokens=args.max_tokens,
                                           stream=True, stream_options={"include_usage": True},
                                           extra_headers=hdr):
        n += 1
        for ch in chunk.get("choices") or []:
            piece = (ch.get("delta") or {}).get("content")
            if piece:
                print(piece, end="", flush=True)
        if chunk.get("usage"):
            usage = chunk["usage"]
    print()
    print(json.dumps({"chunks": n, "usage": usage}, ensure_ascii=False), file=sys.stderr)
    return 0


def main(argv=None) -> int:
    ap = parser()
    args = ap.parse_args(argv)
    if not args.ns:
        ap.print_help()
        return 2
    if args.ns == "commands":
        sel = [o for o in sorted(OPERATIONS.values(), key=lambda o: o["id"])
               if args.ops_ns in (None, o["ns"], o["ns"].replace("_", "-"))]
        for o in sel:
            print(f"{_cmd(o['ns'])} {_cmd(o['name']):40s} {o['method']:6s} {o['path']}")
        return 0
    c = client_from(args)
    try:
        if args.ns == "chat":
            return run_chat(c, args)
        op = getattr(args, "_op", None)
        if op is None:
            ap.parse_args([args.ns, "-h"])
            return 2
        pp = {p["name"]: getattr(args, p["py"]) for p in op["path_params"]}
        qq = {q["name"]: getattr(args, "q_" + q["py"]) for q in op["query_params"]}
        body = build_body(args) if op["body"] else None
        if args.verbose:
            print(f"> {op['method']} {op['path']} {json.dumps(body) if body else ''}", file=sys.stderr)
        out = c.call(op, pp, qq, body)
        if hasattr(out, "__next__"):
            for ev in out:
                emit(ev, args.query)
        else:
            emit(out, args.query)
        return 0
    except RateLimitError as e:
        print(f"error: {e} (retry-after: {e.retry_after})", file=sys.stderr)
        return 3
    except APIStatusError as e:
        print(f"error: {e}", file=sys.stderr)
        if e.body is not None:
            print(json.dumps(e.body, ensure_ascii=False, default=str), file=sys.stderr)
        return 1
    except APIConnectionError as e:
        print(f"error: {e}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    sys.exit(main())
