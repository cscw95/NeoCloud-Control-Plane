#!/usr/bin/env python3
"""US-02..US-05 reproduced with the generated SDK only (F29 done criterion).

    python packaging/examples/stories.py --base-url http://127.0.0.1:8000 \
        --nico-url http://127.0.0.1:9000 [--only us02,us05] [--json]

Each story returns checks `(ok, detail_ko, detail_en)`; ok=None means "not checkable
here" (e.g. NICo URL not given). Exit code 1 when any check is False.
Acceptance criteria (IMPROVEMENT-PLAN-v2.md §3):
  US-02 activate a contract, order with contract_id → 4 racks in one IB zone reserved,
        billing starts at delivery, on/after the contract start date.
  US-03 order to delivery → zero `/nico-bridge` calls on NICo; release → IB partition DELETEd.
  US-04 2-rack node pool → 144 GPUs, 2 clique label values, 2 ComputeDomains, kubeconfig.
  US-05 base_url swap, stream=true → SSE tokens then usage; over TPM → 429 + Retry-After.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path
from typing import Callable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sdk"))

from neocloud_sdk import Client, ConflictError, RateLimitError  # noqa: E402

NICO_PREFIX = "/v2/org/neocloud/nico"
GPUS_PER_TRAY = 4      # Vera Rubin NVL72 compute tray: 2 Vera CPUs + 4 Rubin GPUs (PLAN.md)


def _tenant(c: Client, name: str, tier: str = "bare_metal_dedicated") -> str:
    try:
        return c.tenants.create_tenant({"name": name, "isolation_tier": tier})["id"]
    except ConflictError:
        return f"tnt-{name}"


def _nico(nico_url: Optional[str], path: str):
    if not nico_url:
        return None
    try:
        with urllib.request.urlopen(nico_url.rstrip("/") + path, timeout=30) as r:  # noqa: S310
            return json.loads(r.read() or b"null")
    except (OSError, ValueError):     # NICo unreachable (offline build): not checkable here
        return None


def us02(c: Client, start_date: str = "2026-11-01", tenant: str = "sdk-us02", **_) -> list:
    tid = _tenant(c, tenant)
    ctr = c.contracts.create_contract({
        "tenant_id": tid, "product": "bmaas", "commitment": "reserved", "racks": 4,
        "blueprint_key": "vr-nvl72", "term_months": 12, "start_date": start_date,
        "unit_price": 95_000_000, "placement": {"same_ib_zone": True}})
    act = c.contracts.activate_contract(ctr["id"])
    order = c.orders.create_order({"tenant_id": tid, "kind": "new", "contract_id": ctr["id"]})
    allocs = [a for a in c.tenants.get_tenant(tid).get("allocations") or []
              if a.get("id") in (order.get("allocation_ids") or [])]
    racks = sorted({r for a in allocs for r in a.get("rack_ids") or []})
    zones = sorted({a.get("su_id") for a in allocs})
    line = next((o for o in c.contracts.get_contract(ctr["id"]).get("orders") or []
                 if o.get("order_id") == order["id"]), {})
    bs = (line.get("billing_start") or "")[:10]
    return [
        (act.get("status") == "active", f"계약 {ctr['id']} 활성 ({act.get('start_date')}~{act.get('end_date')})",
         f"contract {ctr['id']} active ({act.get('start_date')}..{act.get('end_date')})"),
        (order.get("state") == "delivered" and len(racks) == 4 and len(zones) == 1,
         f"주문 {order['id']} {order.get('state')} · 랙 {len(racks)}개 · IB 구역 {zones}",
         f"order {order['id']} {order.get('state')} · {len(racks)} racks · IB zone {zones}"),
        (bool(bs) and bs >= start_date and line.get("billing_start") == line.get("delivered_at"),
         f"과금 시작 {bs} = 인도일, 계약 시작 {start_date} 이후",
         f"billing starts {bs} = delivery, on/after contract start {start_date}"),
    ]


def us03(c: Client, nico_url: Optional[str] = None, tenant: str = "sdk-us03", **_) -> list:
    tid = _tenant(c, tenant)
    b0 = _nico(nico_url, "/emulator/v1/bridge-calls")
    order = c.orders.create_order({"tenant_id": tid, "kind": "new", "blueprint_key": "vr-nvl72", "racks": 1})
    parts = _nico(nico_url, f"{NICO_PREFIX}/infiniband-partition?tenantId={tid}")
    out = [(order.get("state") == "delivered", f"1랙 주문 {order['id']} {order.get('state')}",
            f"1-rack order {order['id']} {order.get('state')}")]
    out.append((None, "NICo URL 없음", "no NICo URL") if parts is None else
               (len(parts) == 1, f"NICo IB 파티션 {len(parts)}개 생성", f"{len(parts)} NICo IB partition(s)"))
    term = [c.orders.create_order({"tenant_id": tid, "kind": "terminate", "allocation_id": a})
            for a in order.get("allocation_ids") or []]
    out.append((bool(term) and all(t.get("state") == "closed" for t in term),
                f"반납 주문 {[t['id'] for t in term]} closed", f"release orders {[t['id'] for t in term]} closed"))
    after = _nico(nico_url, f"{NICO_PREFIX}/infiniband-partition?tenantId={tid}")
    b1 = _nico(nico_url, "/emulator/v1/bridge-calls")
    if after is None:
        out.append((None, "NICo URL 없음", "no NICo URL"))
    else:
        n = (b1 or {}).get("count", 0) - (b0 or {}).get("count", 0)
        out.append((len(after) == 0, f"반납 후 파티션 {len(after)}개 (DELETE 완료)",
                    f"{len(after)} partition(s) after release (DELETEd)"))
        out.append((n == 0, f"/nico-bridge 호출 {n}건", f"{n} /nico-bridge call(s)"))
    return out


def us04(c: Client, tenant: str = "sdk-us04", as_tenant: Optional[Callable] = None, **_) -> list:
    tid = _tenant(c, tenant)
    cl = c.nvke.create_cluster({"name": tenant, "tenant_id": tid, "k8s_version": "1.33",
                                "nodepools": [{"name": "gpu", "instance_type": "vr-nvl72", "count": 36}]})
    nodes = c.nvke.list_nodes(cl["id"])
    nodes = nodes.get("nodes", nodes) if isinstance(nodes, dict) else nodes
    cliques = sorted({(n.get("labels") or {}).get("nvidia.com/gpu.clique") for n in nodes} - {None})
    doms = c.nvke.list_compute_domains(cl["id"])
    doms = doms.get("items", doms) if isinstance(doms, dict) else doms
    # kubeconfig is released to the owning tenant only: tenant token, or X-Tenant-Id on dev stacks
    owner = as_tenant(tid) if as_tenant else c.with_options(operator=False, tenant_id=tid)
    kc = owner.nvke.kubeconfig(cl["id"])
    text = kc.get("kubeconfig", "") if isinstance(kc, dict) else str(kc)
    gpus = len(nodes) * GPUS_PER_TRAY
    return [
        (len(nodes) == 36 and gpus == 144, f"노드 {len(nodes)}대 · GPU {gpus}개", f"{len(nodes)} nodes · {gpus} GPUs"),
        (len(cliques) == 2, f"nvidia.com/gpu.clique 값 {len(cliques)}종", f"{len(cliques)} nvidia.com/gpu.clique values"),
        (len(doms) == 2, f"ComputeDomain {len(doms)}개", f"{len(doms)} ComputeDomains"),
        ("kind: Config" in text, "kubeconfig 발급 (소유 테넌트)", "kubeconfig issued to the owning tenant"),
    ]


def us05(c: Client, tenant: str = "sdk-us05", **_) -> list:
    tid = _tenant(c, tenant, tier="k8s_namespace")
    key = c.token.create_key({"tenant_id": tid, "name": "us05"})["key"]
    oa = c.openai_client(key)                    # == OpenAI(base_url=<nocp>/v1, api_key=key)
    model = oa.models.list().data[0].id
    msgs = [{"role": "user", "content": "NVL72 랙 하나에 GPU가 몇 개인가요?"}]
    chunks = list(oa.chat.completions.create(model=model, messages=msgs, max_tokens=16, stream=True,
                                             stream_options={"include_usage": True}))
    text = "".join((ch.choices[0].delta.get("content") or "") for ch in chunks if ch.get("choices"))
    usage = chunks[-1].get("usage") if chunks else None
    c.token.put_quota(tid, {"tier": "interactive", "tpm": 40, "rpm": 1})
    status, retry = None, None
    for _ in range(3):
        try:
            oa.chat.completions.create(model=model, messages=msgs, max_tokens=16)
        except RateLimitError as e:
            status, retry = 429, e.response.headers.get("retry-after")
            break
    return [
        (len(chunks) > 1 and bool(text), f"SSE 청크 {len(chunks)}개 · 본문 {len(text)}자 ({model})",
         f"{len(chunks)} SSE chunks · {len(text)} chars ({model})"),
        (bool(usage) and "prompt_tokens_details" in usage,
         f"마지막 청크 usage {json.dumps(usage, ensure_ascii=False)[:90] if usage else None}",
         f"usage in the last chunk {json.dumps(usage)[:90] if usage else None}"),
        (status == 429 and bool(retry), f"TPM/RPM 초과 → {status} · Retry-After {retry}s",
         f"over TPM/RPM → {status} · Retry-After {retry}s"),
    ]


STORIES: dict[str, Callable[..., list]] = {"us02": us02, "us03": us03, "us04": us04, "us05": us05}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base-url", default=None)
    ap.add_argument("--nico-url", default=None)
    ap.add_argument("--token", default=None)
    ap.add_argument("--only", default=",".join(STORIES))
    ap.add_argument("--suffix", default="", help="tenant name suffix (re-runs on one stack)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    c = Client(a.base_url, token=a.token, operator=True)
    report, bad = {}, 0
    for sid in a.only.split(","):
        checks = STORIES[sid](c, nico_url=a.nico_url, tenant=f"sdk-{sid}{a.suffix}")
        report[sid] = [{"ok": ok, "ko": ko, "en": en} for ok, ko, en in checks]
        bad += sum(1 for ok, _, _ in checks if ok is False)
        if not a.json:
            print(f"== {sid.upper()}")
            for ok, ko, en in checks:
                print(f"  [{'PASS' if ok else 'SKIP' if ok is None else 'FAIL'}] {ko} / {en}")
    if a.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
