"""Shared step builders for the flagship demo scenarios (not a scenario itself)."""

from __future__ import annotations

from ..engine import check, http


def link(console: str, route: str, ko: str, en: str) -> dict:
    """Deep link into a NeoCloud console screen (Demo Center renders it)."""
    return {"console": console, "route": route, "label_ko": ko, "label_en": en}


def tenant_step(sid: str, name: str, key: str = "tenant_id",
                tier: str = "bare_metal_dedicated") -> dict:
    """Create (or reuse after a previous run) a demo tenant via POST /api/v1/tenants."""

    def expect(res, ctx):
        ok = res.status in (201, 409)
        return check(ok, f"테넌트 tnt-{name} 준비 (HTTP {res.status})",
                     f"tenant tnt-{name} ready (HTTP {res.status})")

    def save(res, ctx):
        ctx[key] = res.body["id"] if res.status == 201 else f"tnt-{name}"

    return {"id": sid, "title_ko": f"테넌트 {name} 생성", "title_en": f"Create tenant {name}",
            "narration_ko": "고객 조직을 만들면 VRF·L3VNI·IAM realm이 자동으로 묶인다.",
            "narration_en": "Creating the customer org binds a VRF, an L3VNI and an IAM realm.",
            "call": http("POST", "/api/v1/tenants",
                         {"name": name, "isolation_tier": tier}),
            "expect": expect, "save": save,
            "links": [link("ops", "orchestration", "운영 · 테넌트", "Ops · tenants")]}


def order_step(sid: str, racks: int, key: str = "tenant_id", out: str = "order_id",
               blueprint: str = "vr-nvl72") -> dict:
    def expect(res, ctx):
        b = res.body if isinstance(res.body, dict) else {}
        n = len(b.get("node_ids") or [])
        ok = res.status == 201 and b.get("state") == "delivered" and n == racks * 18
        return check(ok, f"주문 {b.get('id')} → {b.get('state')} · 노드 {n}대 (기대 {racks * 18})",
                     f"order {b.get('id')} → {b.get('state')} · {n} nodes (want {racks * 18})")

    def save(res, ctx):
        ctx[out] = res.body["id"]
        ctx[out + "_alloc"] = (res.body.get("allocation_ids") or [None])[0]
        ctx[out + "_node"] = res.body["node_ids"][0]

    return {"id": sid, "title_ko": f"{racks}랙 BMaaS 주문", "title_en": f"Order {racks} BMaaS racks",
            "narration_ko": "주문은 NICo 할당 → PXE·OS 설치 → 3-plane 격리 → VAST 뷰 연결까지 한 번에 간다.",
            "narration_en": "An order runs NICo allocation → PXE/OS → 3-plane isolation → VAST view.",
            "call": http("POST", "/api/v1/orders",
                         lambda c: {"tenant_id": c[key], "kind": "new",
                                    "blueprint_key": blueprint, "racks": racks}),
            "expect": expect, "save": save,
            "links": [link("ops", "provisioning", "운영 · 프로비저닝", "Ops · provisioning")]}


def first_node_host(ctx: dict, order_key: str = "order_id") -> str:
    """NICo host id of the order's first node (ni-… → nh-…)."""
    return "nh-" + ctx[order_key + "_node"].removeprefix("ni-")


def first_tray(ctx: dict, order_key: str = "order_id") -> str:
    return ctx[order_key + "_node"].removeprefix("ni-")


def rack_of_tray(tray: str) -> str:
    return tray.rsplit("-tray-", 1)[0]
