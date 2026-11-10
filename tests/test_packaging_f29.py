"""F29 packaging: generated SDK, CLI, emulated Terraform provider (packaging/).

Story tests (US-02..05) need F06/F01/F02 routes; they skip on a tree without them and
run once the feature branches are merged.
"""

import io
import json
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "packaging"
for p in (PKG / "sdk", PKG / "cli", PKG / "terraform", PKG / "examples"):
    sys.path.insert(0, str(p))

import gen  # noqa: E402
import provider as tfp  # noqa: E402
import tfemu  # noqa: E402
from neocloud_cli import __main__ as cli  # noqa: E402
from neocloud_sdk import (OPERATIONS, AuthenticationError, Client, NotFoundError,  # noqa: E402
                          RateLimitError, Response, TestClientTransport)
from neocloud_sdk._transport import iter_sse  # noqa: E402
from neocloud_sdk.openai_compat import OpenAI  # noqa: E402

from app.main import app  # noqa: E402

STORY_OPS = {("POST", "/api/v1/contracts"), ("POST", "/api/v1/contracts/{contract_id}/activate"),
             ("POST", "/api/v1/orders"), ("POST", "/api/v1/nvke/clusters"),
             ("GET", "/api/v1/nvke/clusters/{cluster_id}/kubeconfig"),
             ("GET", "/api/v1/nvke/clusters/{cluster_id}/compute-domains"),
             ("POST", "/api/v1/token/keys"), ("PUT", "/api/v1/token/quotas/{tenant_id}"),
             ("POST", "/v1/chat/completions"), ("GET", "/v1/models")}


def _routes():
    return {(o["method"], o["path"]) for o in gen.collect(app.openapi())}


def has(*ops):
    return all(o in _routes() for o in ops)


@pytest.fixture()
def nc(client):
    return Client("http://testserver", operator=True, transport=TestClientTransport(client))


# --------------------------------------------------------------------------- generator
def test_generated_module_matches_snapshot():
    assert gen.main(["--check"]) == 0


def test_sdk_covers_story_operations_and_this_tree():
    sdk = {(o["method"], o["path"]) for o in OPERATIONS.values()}
    assert STORY_OPS <= sdk
    # every route of origin/main must be in the SDK (later features: re-run gen.py --from-app)
    base = {("GET", "/api/v1/tenants"), ("POST", "/api/v1/tenants"), ("GET", "/api/v1/nvke/releases"),
            ("POST", "/api/v1/api-keys"), ("GET", "/api/v1/orders/{order_id}")}
    assert base <= sdk


def test_naming_is_deterministic_and_unique():
    ops = gen.collect(json.loads(gen.SNAPSHOT.read_text()))
    ids = [o["id"] for o in ops]
    assert len(ids) == len(set(ids))
    assert OPERATIONS["contracts.create_contract"]["path"] == "/api/v1/contracts"
    assert OPERATIONS["openai.chat_completions"]["body"] == "json"
    assert gen.namespace("/realms/x/protocol/openid-connect/token") == "oidc"
    assert gen.handler_name("get_order_api_v1_orders__order_id__get", "/api/v1/orders/{order_id}", "get") == "get_order"


def test_generated_bodies_follow_openapi_request_schemas():
    spec = json.loads(gen.SNAPSHOT.read_text())
    for op in OPERATIONS.values():
        raw = spec["paths"][op["path"]][op["method"].lower()]
        assert bool(raw.get("requestBody")) == bool(op["body"]), op["id"]
        names = {p["name"] for p in raw.get("parameters", []) if p["in"] == "path"}
        assert names == {p["name"] for p in op["path_params"]}, op["id"]


# --------------------------------------------------------------------------- SDK core
def test_sdk_roundtrip_and_error_mapping(nc):
    t = nc.tenants.create_tenant({"name": "f29-sdk", "isolation_tier": "bare_metal_dedicated"})
    assert t["id"] == "tnt-f29-sdk"
    assert nc.tenants.get_tenant(t["id"])["tenant"]["name"] == "f29-sdk"
    assert nc.last_request["method"] == "GET" and nc.history[-1]["status"] == 200
    with pytest.raises(NotFoundError) as e:
        nc.orders.get_order("ord-nope")
    assert e.value.status == 404
    anon = Client("http://testserver", token="bogus", transport=nc.transport)
    with pytest.raises(AuthenticationError):
        anon.tenants.list_tenants()


def test_sse_parser_follows_eventsource_rules():
    lines = [": keep-alive", "data: {\"a\":1}", "", "event: x", "data: 2", "data: 3", "", "data: [DONE]", "", "data: 9"]
    assert list(iter_sse(iter(lines))) == [{"event": "message", "data": "{\"a\":1}"},
                                           {"event": "x", "data": "2\n3"}]


class FakeTransport:
    """Replays OpenAI-shaped responses; 429 on the 2nd non-stream call."""

    def __init__(self):
        self.calls = []
        self.plain = 0

    def send(self, method, url, headers, body, stream=False):
        self.calls.append((method, url, headers))
        if url.endswith("/v1/models"):
            return Response(200, {"content-type": "application/json"},
                            {"object": "list", "data": [{"id": "m1", "object": "model", "created": 0, "owned_by": "nc"}]})
        req = json.loads(body)
        if req.get("stream"):
            ev = [{"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "m1",
                   "choices": [{"index": 0, "delta": {"role": "assistant", "content": "hi"}, "finish_reason": None}]},
                  {"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "m1", "choices": [],
                   "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4,
                             "prompt_tokens_details": {"cached_tokens": 0},
                             "completion_tokens_details": {"reasoning_tokens": 0}}}]
            text = "".join(f"data: {json.dumps(e)}\n\n" for e in ev) + "data: [DONE]\n\n"
            return Response(200, {"content-type": "text/event-stream"}, None, b"", iter(text.splitlines()))
        self.plain += 1
        if self.plain >= 2:
            return Response(429, {"retry-after": "7", "content-type": "application/json"},
                            {"error": {"message": "Rate limit reached", "type": "requests", "code": "rate_limit_exceeded"}})
        return Response(200, {"content-type": "application/json"},
                        {"id": "c2", "object": "chat.completion", "created": 1, "model": "m1",
                         "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok", "refusal": None},
                                      "finish_reason": "stop", "logprobs": None}],
                         "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4}})


def test_openai_compat_surface_and_rate_limit():
    ft = FakeTransport()
    oa = OpenAI(base_url="http://nocp/v1", api_key="sk-neo-x", transport=ft)
    assert oa.models.list().data[0].id == "m1"
    chunks = list(oa.chat.completions.create(model="m1", messages=[], stream=True))
    assert chunks[0].choices[0].delta.content == "hi"
    assert chunks[-1].usage.prompt_tokens_details.cached_tokens == 0
    assert ft.calls[-1][2]["Authorization"] == "Bearer sk-neo-x"
    assert ft.calls[-1][2]["Accept"] == "text/event-stream"
    assert oa.chat.completions.create(model="m1", messages=[]).choices[0].message.content == "ok"
    with pytest.raises(RateLimitError) as e:
        oa.chat.completions.create(model="m1", messages=[])
    assert e.value.response.headers["retry-after"] == "7" and e.value.retry_after == 7.0


# --------------------------------------------------------------------------- OpenAI contract
def _openai_spec():
    for base in [ROOT, *ROOT.parents]:
        f = base / "ref" / "upstream" / "openai" / "openapi.yaml"
        if f.exists():
            return yaml.safe_load(f.read_text())
    pytest.skip("ref/upstream/openai/openapi.yaml not found")


def _check(schema, value, spec, path="$"):
    """Small OpenAPI 3 validator: $ref, allOf/anyOf/oneOf, type, required, properties."""
    if "$ref" in schema:
        return _check(spec["components"]["schemas"][schema["$ref"].rsplit("/", 1)[-1]], value, spec, path)
    if "allOf" in schema:
        return [e for s in schema["allOf"] for e in _check(s, value, spec, path)]
    for k in ("anyOf", "oneOf"):
        if k in schema:
            alts = [_check(s, value, spec, path) for s in schema[k]]
            return [] if any(not a for a in alts) else alts[0]
    t = schema.get("type")
    if value is None:
        return [] if schema.get("nullable") or t in (None, "null") or (isinstance(t, list) and "null" in t) else [f"{path}: null"]
    py = {"object": dict, "array": list, "string": str, "integer": int, "number": (int, float), "boolean": bool}
    types = t if isinstance(t, list) else [t] if t else []
    if types and not any(x == "null" or isinstance(value, py.get(x, object)) for x in types):
        return [f"{path}: {type(value).__name__} is not {t}"]
    errs = []
    if isinstance(value, dict):
        errs += [f"{path}.{r}: missing" for r in schema.get("required", []) if r not in value]
        for k, s in (schema.get("properties") or {}).items():
            if k in value:
                errs += _check(s, value[k], spec, f"{path}.{k}")
    if isinstance(value, list) and "items" in schema:
        for i, v in enumerate(value):
            errs += _check(schema["items"], v, spec, f"{path}[{i}]")
    return errs


@pytest.mark.skipif(not has(("POST", "/v1/chat/completions"), ("POST", "/api/v1/token/keys")),
                    reason="F01 token gateway not merged")
def test_sdk_stream_matches_openai_schema(nc):
    spec = _openai_spec()
    nc.tenants.create_tenant({"name": "f29-oa", "isolation_tier": "k8s_namespace"})
    oa = nc.openai_client(nc.token.create_key({"tenant_id": "tnt-f29-oa", "name": "t"})["key"])
    model = oa.models.list().data[0].id
    chunks = list(oa.chat.completions.create(model=model, max_tokens=4, stream=True,
                                             messages=[{"role": "user", "content": "hi"}],
                                             stream_options={"include_usage": True}))
    for ch in chunks:
        assert _check({"$ref": "#/components/schemas/CreateChatCompletionStreamResponse"}, dict(ch), spec) == []
    usage = chunks[-1]["usage"]
    assert _check({"$ref": "#/components/schemas/CompletionUsage"}, usage, spec) == []
    full = oa.chat.completions.create(model=model, max_tokens=4, messages=[{"role": "user", "content": "hi"}])
    assert _check({"$ref": "#/components/schemas/CreateChatCompletionResponse"}, dict(full), spec) == []


# --------------------------------------------------------------------------- CLI
def _cli(client, *argv):
    out, err = io.StringIO(), io.StringIO()
    orig = cli.client_from
    cli.client_from = lambda a: Client("http://testserver", token=a.token, api_key=a.api_key,
                                       operator=a.operator, tenant_id=a.tenant,
                                       transport=TestClientTransport(client))
    try:
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(list(argv))
    finally:
        cli.client_from = orig
    return rc, out.getvalue(), err.getvalue()


def test_cli_generated_commands_fields_and_query(client):
    rc, out, _ = _cli(client, "commands", "--ns", "contracts")
    assert rc == 0 and "create-contract" in out
    rc, out, _ = _cli(client, "--operator", "tenants", "create-tenant", "-f", "name=f29-cli",
                      "-f", "isolation_tier=bare_metal_dedicated", "-q", "id")
    assert rc == 0 and out.strip() == "tnt-f29-cli"
    rc, out, _ = _cli(client, "--operator", "tenants", "get-tenant", "tnt-f29-cli", "-q", "tenant.name")
    assert rc == 0 and out.strip() == "f29-cli"
    rc, _, err = _cli(client, "--operator", "orders", "get-order", "ord-x")
    assert rc == 1 and "404" in err


def test_cli_body_builder_nests_and_types():
    ns = type("A", (), {"body": '{"a": 1}', "field": ["s=4"], "raw_field": ["placement.same_ib_zone=true", "n=4"]})
    assert cli.build_body(ns) == {"a": 1, "s": "4", "placement": {"same_ib_zone": True}, "n": 4}
    assert cli.pick({"x": [{"y": 2}]}, "x.0.y") == 2 and cli.pick({"x": [1, 2]}, "x.length") == 2


def test_cli_rate_limit_exit_code(monkeypatch):
    ft = FakeTransport()
    ft.plain = 1                                             # next chat call → 429
    monkeypatch.setattr(cli, "client_from", lambda a: Client("http://nocp", api_key="k", transport=ft))
    err = io.StringIO()
    with redirect_stderr(err), redirect_stdout(io.StringIO()):
        rc = cli.main(["--api-key", "k", "chat", "-m", "m1", "hi"])
    assert rc == 3 and "retry-after: 7.0" in err.getvalue()


# --------------------------------------------------------------------------- Terraform
def test_provider_schema_document_is_current_and_well_formed():
    doc = tfp.providers_schema()
    assert json.loads((PKG / "terraform" / "provider-schema.json").read_text()) == doc
    assert doc["format_version"] == "1.0"
    ps = doc["provider_schemas"]["registry.terraform.io/neocloud/neocloud"]
    assert set(ps) == {"provider", "resource_schemas", "data_source_schemas"}
    for rtype, rs in ps["resource_schemas"].items():
        assert rtype.startswith("neocloud_") and rs["version"] == 0
        assert "id" in rs["block"]["attributes"]
        for name, a in rs["block"]["attributes"].items():
            assert "type" in a, (rtype, name)
            assert a.get("required") or a.get("optional") or a.get("computed"), (rtype, name)
            assert not (a.get("required") and (a.get("optional") or a.get("computed"))), (rtype, name)


@pytest.mark.parametrize("ex", ["us02-reserved-contract", "us04-nvke-2rack", "us05-token-api"])
def test_examples_validate_and_hcl_twin_in_sync(ex):
    d = PKG / "terraform" / "examples" / ex
    assert (d / "main.tf").read_text() == tfemu.to_hcl(d)
    assert tfemu.main([f"-chdir={d}", "init"]) == 0
    assert tfemu.main([f"-chdir={d}", "validate"]) == 0


def test_tfemu_plan_apply_replace_destroy_state_v4(tmp_path, nc):
    cfg = {"terraform": {"required_providers": {"neocloud": {"source": "neocloud/neocloud"}}},
           "variable": {"n": {"type": "string", "default": "f29-tf"}},
           "resource": {"neocloud_tenant": {"a": {"name": "${var.n}"}}},
           "output": {"tid": {"value": "${neocloud_tenant.a.id}"}}}
    (tmp_path / "main.tf.json").write_text(json.dumps(cfg))
    c = tfemu.load_config(tmp_path)
    v = tfemu.variables_of(c, {})
    st = tfemu.load_state(tmp_path)
    p = tfemu.plan(c, st, v)
    assert [x["change"]["actions"] for x in p] == [["create"]]
    assert p[0]["change"]["after_unknown"]["id"] is True
    tfemu.apply(tmp_path, c, st, v, quiet=True, client=nc)
    s = json.loads((tmp_path / "terraform.tfstate").read_text())
    assert s["version"] == 4 and s["serial"] >= 1 and s["lineage"]
    r = s["resources"][0]
    assert r["mode"] == "managed" and r["provider"] == 'provider["registry.terraform.io/neocloud/neocloud"]'
    assert r["instances"][0]["attributes"]["id"] == "tnt-f29-tf"
    assert s["outputs"]["tid"] == {"value": "tnt-f29-tf", "type": "string", "sensitive": False}
    assert tfemu.summary(tfemu.plan(c, s, v)) == "Plan: 0 to add, 0 to change, 0 to destroy."
    v2 = tfemu.variables_of(c, {"n": "f29-tf2"})
    assert [x["change"]["actions"] for x in tfemu.plan(c, s, v2)] == [["delete", "create"]]
    pj = tfemu.plan_json(tfemu.plan(c, s, v, destroy=True), c)
    assert pj["format_version"] == "1.2" and pj["resource_changes"][0]["change"]["actions"] == ["delete"]
    tfemu.apply(tmp_path, c, s, v, destroy=True, quiet=True, client=nc)
    assert json.loads((tmp_path / "terraform.tfstate").read_text())["resources"] == []


def test_tfemu_rejects_unknown_attributes(tmp_path):
    assert tfp.validate("neocloud_tenant", {"name": "x", "bogus": 1}) == ['Unsupported argument "bogus" in neocloud_tenant']
    assert tfp.validate("neocloud_contract", {})[0].startswith('The argument "tenant_id" is required')


# --------------------------------------------------------------------------- stories (US-02..05)
@pytest.mark.skipif(not has(("POST", "/api/v1/contracts")), reason="F06 contracts not merged")
def test_us02_via_sdk(nc):
    import stories
    checks = stories.us02(nc, tenant="f29-us02")
    assert all(ok for ok, _, _ in checks), checks


@pytest.mark.skipif(not has(("GET", "/api/v1/nvke/clusters/{cluster_id}/compute-domains")),
                    reason="F11 NVKE depth not merged")
def test_us04_via_sdk(nc):
    import stories
    checks = stories.us04(nc, tenant="f29-us04")
    assert all(ok for ok, _, _ in checks), checks


@pytest.mark.skipif(not has(("POST", "/v1/chat/completions"), ("PUT", "/api/v1/token/quotas/{tenant_id}")),
                    reason="F01/F02 not merged")
def test_us05_via_sdk(nc):
    import stories
    checks = stories.us05(nc, tenant="f29-us05")
    assert all(ok for ok, _, _ in checks), checks


def test_us03_order_and_release_via_sdk(nc):
    import stories
    checks = stories.us03(nc, tenant="f29-us03")          # no NICo URL in the unit suite
    assert [ok for ok, _, _ in checks] == [True, None, True, None]


# --------------------------------------------------------------------------- demo runner
def test_f29_demo_scenario_runs_without_failures(client):
    pytest.importorskip("app.demo.engine", reason="demo runner (feat/v2-demo) not merged")
    client.post("/api/v1/demo/reset", json={})     # Phase-1 site layout: room for US-02 + US-03 + US-04
    r = client.post("/api/v1/demo/scenarios/f29-packaging/run", params={"wait": True}, json={"mode": "all"})
    assert r.status_code == 201, r.text
    steps = r.json()["steps"]
    assert [s["id"] for s in steps][:2] == ["00-sdk-sync", "01-us02"]
    failed = [(s["id"], s["assertion"]) for s in steps if s["state"] == "failed"]
    assert failed == [], failed
    tf = next(s for s in steps if s["id"] == "05-terraform")
    assert tf["state"] in ("passed", "pending"), tf
