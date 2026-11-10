"""F29 demo: US-02..US-05 driven through the generated Python SDK and the emulated
Terraform provider (packaging/), in-process over the runner's NOCP app.

Each step runs one user story from packaging/examples/stories.py with an SDK client
whose transport is a TestClient on this app (operator HMAC token; tenant token for
the kubeconfig), and shows the SDK's wire requests. The last step applies the
Terraform example us05-token-api with tfemu into a temp dir and checks the v4 state.
Terminal twin: scripts/demo_f29.sh (CLI + Terraform + SDK against a live stack).
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from shutil import copyfile

from ..engine import Result

PKG = Path(__file__).resolve().parents[3] / "packaging"
for p in (PKG / "sdk", PKG / "examples", PKG / "terraform"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


def _client(tenant_id=None):
    from fastapi.testclient import TestClient
    from neocloud_sdk import Client, TestClientTransport

    from ... import auth
    from ...main import app
    tok = (auth.mint("tenant", tenant_id=tenant_id, tenant_role="admin", ttl_s=900) if tenant_id
           else auth.mint("operator", ttl_s=900))
    tc = TestClient(app, base_url="http://nocp.demo", raise_server_exceptions=False)
    return Client("http://nocp.demo", token=tok, transport=TestClientTransport(tc))


def _nico_url():
    from ... import dsx
    return dsx.nico_url()


def _story(sid: str):
    def call(ctx):
        import stories
        c = _client()
        try:
            checks = stories.STORIES[sid](c, nico_url=_nico_url(), tenant=f"demo-f29-{sid}",
                                          as_tenant=lambda tid: _client(tid))
        except Exception as e:  # surface SDK errors as a failed step, not a crash
            return Result(599, {"error": f"{type(e).__name__}: {e}", "wire": c.history[-8:]})
        return Result(200, {"checks": [{"ok": ok, "ko": ko, "en": en} for ok, ko, en in checks],
                            "wire": c.history[-12:]})
    call.__qualname__ = f"packaging/examples/stories.py:{sid}"
    return call


def _all_ok(res, ctx):
    b = res.body if isinstance(res.body, dict) else {}
    if res.status != 200:
        return (False, f"SDK 오류: {b.get('error')}", f"SDK error: {b.get('error')}")
    ch = b.get("checks") or []
    bad = [c for c in ch if c["ok"] is False]
    ok = None if all(c["ok"] is None for c in ch) else not bad
    return (ok, " · ".join(c["ko"] for c in ch), " · ".join(c["en"] for c in ch))


def _sdk_sync(ctx):
    import gen
    from neocloud_sdk import OPERATIONS

    from ...main import app
    live = {(o["method"], o["path"]) for o in gen.collect(app.openapi())}
    sdk = {(o["method"], o["path"]) for o in OPERATIONS.values()}
    need = {("POST", "/api/v1/contracts"), ("POST", "/api/v1/contracts/{contract_id}/activate"),
            ("POST", "/api/v1/orders"), ("POST", "/api/v1/nvke/clusters"),
            ("GET", "/api/v1/nvke/clusters/{cluster_id}/kubeconfig"), ("POST", "/api/v1/token/keys"),
            ("PUT", "/api/v1/token/quotas/{tenant_id}"), ("POST", "/v1/chat/completions")}
    return Result(200, {"sdk_operations": len(sdk), "live_operations": len(live),
                        "missing_in_sdk": sorted(f"{m} {p}" for m, p in live - sdk)[:20],
                        "story_ops_missing": sorted(f"{m} {p}" for m, p in need - (sdk & live))})


_sdk_sync.__qualname__ = "packaging/sdk/gen.py:collect(app.openapi()) vs neocloud_sdk.OPERATIONS"


def _sync_ok(res, ctx):
    b = res.body
    ok = not b["story_ops_missing"]
    return (ok, f"SDK {b['sdk_operations']}개 / 서버 {b['live_operations']}개 작업, US 필수 누락 "
                f"{len(b['story_ops_missing'])} · SDK에 없는 작업 {len(b['missing_in_sdk'])}",
            f"SDK {b['sdk_operations']} / server {b['live_operations']} operations, story ops missing "
            f"{len(b['story_ops_missing'])} · not in SDK {len(b['missing_in_sdk'])}")


def _terraform(ctx):
    import tfemu
    c = _client()
    src = PKG / "terraform" / "examples" / "us05-token-api"
    with tempfile.TemporaryDirectory() as d:
        dp = Path(d)
        copyfile(src / "main.tf.json", dp / "main.tf.json")
        cfg = tfemu.load_config(dp)
        variables = tfemu.variables_of(cfg, {"tenant_name": "demo-f29-tf", "base_url": "http://nocp.demo"})
        st = tfemu.load_state(dp)
        plan = tfemu.plan(cfg, st, variables)
        out = tfemu.apply(dp, cfg, st, variables, quiet=True, client=c)
        state = json.loads((dp / "terraform.tfstate").read_text())
        view = {"plan": [f"{x['address']} {x['change']['actions']}" for x in plan],
                "log": out["log"], "state_version": state["version"],
                "resources": [f"{r['type']}.{r['name']}" for r in state["resources"]],
                "outputs": {k: ("<sensitive>" if v["sensitive"] else v["value"])
                            for k, v in state["outputs"].items()},
                "replan": tfemu.summary(tfemu.plan(cfg, state, variables))}
        gone = tfemu.apply(dp, cfg, state, variables, destroy=True, quiet=True, client=c)
    return Result(200, {**view, "destroy_log": gone["log"], "wire": c.history[-8:]})


_terraform.__qualname__ = "packaging/terraform/tfemu.py apply examples/us05-token-api"


def _tf_ok(res, ctx):
    b = res.body
    ok = b["state_version"] == 4 and len(b["resources"]) == 3 and b["replan"].startswith("Plan: 0 to add, 0")
    return (ok, f"state v{b['state_version']} · 리소스 {len(b['resources'])}개 · 재계획 '{b['replan']}'",
            f"state v{b['state_version']} · {len(b['resources'])} resources · re-plan '{b['replan']}'")


SCENARIO = {
    "id": "f29-packaging",
    "order": 29,
    "feature": "F29",
    "story": "US-02 · US-03 · US-04 · US-05",
    "title_ko": "F29 CLI·Terraform·Python SDK로 US-02~05 재현",
    "title_en": "F29 CLI · Terraform · Python SDK reproduce US-02..05",
    "summary_ko": "NOCP OpenAPI에서 생성한 SDK로 계약·주문·NVKE·토큰 API 스토리를 돌리고, Terraform 예제를 적용한다.",
    "summary_en": "The SDK generated from the NOCP OpenAPI runs the contract, order, NVKE and token-API stories; a Terraform example is applied.",
    "steps": [
        {"id": "00-sdk-sync", "title_ko": "SDK ↔ OpenAPI 동기 확인", "title_en": "SDK ↔ OpenAPI in sync",
         "narration_ko": "SDK는 packaging/openapi/nocp-openapi.json에서 생성된다. 서버의 현재 OpenAPI와 비교한다.",
         "narration_en": "The SDK is generated from packaging/openapi/nocp-openapi.json; compare with the live OpenAPI.",
         "call": _sdk_sync, "expect": _sync_ok},
        {"id": "01-us02", "feature": "F06", "requires": ["POST /api/v1/contracts"],
         "title_ko": "US-02 예약 계약 → 주문 (SDK)", "title_en": "US-02 reserved contract → order (SDK)",
         "narration_ko": "nc.contracts.create_contract → activate_contract → nc.orders.create_order(contract_id).",
         "narration_en": "nc.contracts.create_contract → activate_contract → nc.orders.create_order(contract_id).",
         "call": _story("us02"), "expect": _all_ok},
        {"id": "02-us03", "feature": "F08",
         "title_ko": "US-03 NICo REST 할당·반납 (SDK)", "title_en": "US-03 NICo REST allocate / release (SDK)",
         "call": _story("us03"), "expect": _all_ok},
        {"id": "03-us04", "feature": "F11", "requires": ["GET /api/v1/nvke/clusters/{cluster_id}/compute-domains"],
         "title_ko": "US-04 2랙 NVKE 노드풀 (SDK)", "title_en": "US-04 2-rack NVKE node pool (SDK)",
         "call": _story("us04"), "expect": _all_ok},
        {"id": "04-us05", "feature": "F01", "requires": ["POST /v1/chat/completions"],
         "title_ko": "US-05 OpenAI SDK 형태 스트림·429 (SDK)", "title_en": "US-05 OpenAI-SDK-shaped stream · 429 (SDK)",
         "narration_ko": "OpenAI(base_url=…/v1, api_key) 그대로 stream=True, 쿼터를 줄이면 429와 Retry-After.",
         "narration_en": "OpenAI(base_url=…/v1, api_key) with stream=True; a tight quota returns 429 + Retry-After.",
         "call": _story("us05"), "expect": _all_ok},
        {"id": "05-terraform", "requires": ["POST /api/v1/token/keys"],
         "title_ko": "Terraform 예제 적용 (tfemu)", "title_en": "Apply a Terraform example (tfemu)",
         "narration_ko": "examples/us05-token-api: 테넌트·키·쿼터를 plan → apply → 재계획 0건 → destroy.",
         "narration_en": "examples/us05-token-api: tenant, key, quota via plan → apply → empty re-plan → destroy.",
         "call": _terraform, "expect": _tf_ok},
    ],
}
