"""F32 — Marketplace one-click model deploy: catalog → DynamoGraphDeployment on
the Dynamo operator (AI-Infra) → OpenAI-compatible endpoint on the token API.

Self-contained HTTP steps (expect → (ok, detail_ko, detail_en)); runs under the
v2-demo runner and tests/test_marketplace.py. With AI-Infra offline or without
the DGD route the deploy is recorded as engine "stub" and the steps still pass.
The upstream DGD request/response is in the deploy response (`upstream`).
"""

TENANT = "demo-f32-market"
TID = f"tnt-{TENANT}"
MODEL = "llama-3.3-70b-instruct"


def _ok(cond, ko, en):
    return bool(cond), ko, en


def _tenant(res, ctx):
    ctx["tenant_id"] = res.body["id"] if res.status == 201 else TID
    return _ok(res.status in (201, 409), f"테넌트 {ctx['tenant_id']} 준비 (HTTP {res.status})",
               f"tenant {ctx['tenant_id']} ready (HTTP {res.status})")


def _catalog(res, ctx):
    data = (res.body or {}).get("data") or [] if isinstance(res.body, dict) else []
    m = next((x for x in data if x["id"] == MODEL), {})
    p = m.get("price_usd_per_1m_tokens") or {}
    return _ok(res.status == 200 and len(data) >= 4 and m,
               f"카탈로그 {len(data)}개 모델 · {MODEL}: 입력 ${p.get('input')}/1M · 출력 ${p.get('output')}/1M · 엔진 {m.get('engine')}",
               f"catalog of {len(data)} models · {MODEL}: input ${p.get('input')}/1M · output ${p.get('output')}/1M · engine {m.get('engine')}")


def _cleanup_list(res, ctx):
    rows = res.body if isinstance(res.body, list) else []
    ctx["old"] = rows[0]["id"] if rows else "none"
    return _ok(res.status == 200, f"이전 배포 {len(rows)}건", f"{len(rows)} previous deployments")


def _cleaned(res, ctx):
    return _ok(res.status in (200, 404), f"이전 배포 정리 (HTTP {res.status})",
               f"previous deployment cleaned up (HTTP {res.status})")


def _deploy(res, ctx):
    b = res.body if isinstance(res.body, dict) else {}
    if res.status == 201:
        ctx["dep"] = b["id"]
    comps = [c["type"] for c in ((b.get("dgd") or {}).get("spec") or {}).get("components") or []]
    return _ok(res.status == 201 and b.get("state") == "Ready" and comps == ["frontend", "worker"],
               f"원클릭 배포 {b.get('id')} → DGD {b.get('namespace')}/{b.get('name')} ({' + '.join(comps)}) · 엔진 {b.get('engine')} · {b.get('gpus')} GPU",
               f"one-click deploy {b.get('id')} → DGD {b.get('namespace')}/{b.get('name')} ({' + '.join(comps)}) · engine {b.get('engine')} · {b.get('gpus')} GPU")


def _dedicated(res, ctx):
    b = res.body if isinstance(res.body, dict) else {}
    if res.status == 201:
        ctx["dep2"] = b["id"]
    comps = [c["type"] for c in ((b.get("dgd") or {}).get("spec") or {}).get("components") or []]
    pr = b.get("price") or {}
    return _ok(res.status == 201 and comps == ["frontend", "prefill", "decode"],
               f"전용 티어: 분리형 prefill 1 + decode 2 · {b.get('gpus')} GPU · ${pr.get('usd_per_hour')}/시간",
               f"dedicated tier: disaggregated prefill 1 + decode 2 · {b.get('gpus')} GPU · ${pr.get('usd_per_hour')}/hour")


def _endpoint(res, ctx):
    b = res.body if isinstance(res.body, dict) else {}
    ep = b.get("endpoint") or {}
    return _ok(res.status == 200 and ep.get("chat_completions") == "/v1/chat/completions",
               f"엔드포인트 {ep.get('chat_completions')} · model={ep.get('model')} · 게이트웨이 {ep.get('gateway')}",
               f"endpoint {ep.get('chat_completions')} · model={ep.get('model')} · gateway {ep.get('gateway')}")


def _list(res, ctx):
    rows = res.body if isinstance(res.body, list) else []
    return _ok(res.status == 200 and len(rows) == 2,
               f"테넌트 배포 {len(rows)}건: " + ", ".join(f"{r['model']}({r['tier']})" for r in rows),
               f"{len(rows)} tenant deployments: " + ", ".join(f"{r['model']}({r['tier']})" for r in rows))


def _deleted(res, ctx):
    return _ok(res.status == 200, f"전용 배포 삭제 → DGD {(res.body or {}).get('dgd')} 제거",
               f"dedicated deployment deleted → DGD {(res.body or {}).get('dgd')} removed")


def _step(sid, ko, en, method, path, expect, json=None, nko="", nen="", headers=None):
    call = {"method": method, "path": path}
    if json is not None:
        call["json"] = json
    return {"id": sid, "title_ko": ko, "title_en": en, "feature": "F32", "call": call,
            "expect": expect, "narration_ko": nko, "narration_en": nen,
            "links": [{"console": "customer", "route": "marketplace", "label_ko": "고객 · 마켓플레이스",
                       "label_en": "Customer · marketplace"}]}


SCENARIO = {
    "id": "f32-marketplace-deploy",
    "order": 32,
    "feature": "F32",
    "title_ko": "F32 마켓플레이스: 모델 원클릭 배포 → 토큰 API",
    "title_en": "F32 marketplace: one-click model deploy → token API",
    "summary_ko": "카탈로그에서 모델을 골라 배포하면 Dynamo DGD가 만들어지고 /v1/chat/completions 엔드포인트가 나온다.",
    "summary_en": "Picking a catalog model creates a Dynamo DGD and yields a /v1/chat/completions endpoint.",
    "steps": [
        _step("00-tenant", "데모 테넌트", "Demo tenant", "POST", "/api/v1/tenants", _tenant,
              {"name": TENANT, "isolation_tier": "k8s_namespace"}),
        _step("01-catalog", "모델 카탈로그", "Model catalog", "GET", "/api/v1/marketplace/models", _catalog),
        _step("02-old", "이전 배포 확인", "Previous deployments", "GET",
              lambda c: f"/api/v1/marketplace/deployments?tenant_id={c['tenant_id']}", _cleanup_list),
        _step("03-clean-a", "이전 배포 정리", "Clean up", "DELETE",
              lambda c: f"/api/v1/marketplace/deployments/{c['old']}", _cleaned),
        _step("04-old-b", "이전 배포 재확인", "Previous deployments (again)", "GET",
              lambda c: f"/api/v1/marketplace/deployments?tenant_id={c['tenant_id']}", _cleanup_list),
        _step("05-clean-b", "이전 배포 정리", "Clean up", "DELETE",
              lambda c: f"/api/v1/marketplace/deployments/{c['old']}", _cleaned),
        _step("06-deploy", f"원클릭 배포: {MODEL} (서버리스)", f"One-click deploy: {MODEL} (serverless)",
              "POST", "/api/v1/marketplace/deploy", _deploy,
              lambda c: {"model": MODEL, "tier": "serverless", "tenant_id": c["tenant_id"]},
              "NOCP가 DynamoGraphDeployment(nvidia.com/v1beta1)를 Dynamo 오퍼레이터(AI-Infra)에 만든다.",
              "NOCP creates a DynamoGraphDeployment (nvidia.com/v1beta1) on the Dynamo operator (AI-Infra)."),
        _step("07-endpoint", "엔드포인트 확인", "Endpoint", "GET",
              lambda c: f"/api/v1/marketplace/deployments/{c['dep']}", _endpoint,
              nko="OpenAI 호환 /v1/chat/completions 에서 model 이름으로 바로 부른다 (F01 게이트웨이).",
              nen="Call it right away on the OpenAI-compatible /v1/chat/completions by model name (F01 gateway)."),
        _step("08-dedicated", "전용 티어 배포: deepseek-r1", "Dedicated tier: deepseek-r1", "POST",
              "/api/v1/marketplace/deploy", _dedicated,
              lambda c: {"model": "deepseek-r1", "tier": "dedicated", "tenant_id": c["tenant_id"]}),
        _step("09-list", "테넌트 배포 목록", "Tenant deployments", "GET",
              lambda c: f"/api/v1/marketplace/deployments?tenant_id={c['tenant_id']}", _list),
        _step("10-delete", "전용 배포 삭제", "Delete dedicated deployment", "DELETE",
              lambda c: f"/api/v1/marketplace/deployments/{c['dep2']}", _deleted),
    ],
}
