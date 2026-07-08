"""IB fabric topology read-model — spine/leaf(rail)/rack 구성 시각화용.

NCP 레퍼런스의 rail-optimized 2-tier compute fabric을 단순화해 노출한다:
  - 트레이의 SuperNIC n번 포트는 SU 내 rail-n leaf 스위치에 접속
    (rail 수 = blueprint의 connectx_supernics_per_tray),
  - SU의 leaf 그룹은 core(spine) 스위치 전체와 풀메시 업링크,
  - 테넌트 격리는 UFM P_Key로 표현 (NetworkIsolation.ib_pkey).

/nico 대시보드의 SVG 렌더러가 이 read-model을 그대로 그린다. 실제 케이블
단위 SoT는 Nautobot(D3) 몫이며, 여기는 이해·시각화 목적의 요약 모델이다.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter

from . import spec
from .store import STORE

router = APIRouter(prefix="/api/v1", tags=["fabric"])

CORE_SPINES = 4


@router.get("/fabric/ib")
def ib_fabric(tenant_id: Optional[str] = None) -> dict:
    """사이트(AIFactory)별 **독립** IB 패브릭 — 가산·안산 간 크로스 연결 없음.

    NCP RD(RD-12835-001-01 v02) 기준 컴퓨트 패브릭:
      - 듀얼 네트워크 Fabric-A/B (별도 서브넷 → UFM(HA) 2세트),
      - 네트워크당 rail 4 × 리프 16/SU, SU당 2,304×800G 노드-리프 링크,
      - 2-tier ≤9 SU / 3-tier 10~32 SU (컨버지드는 2-tier ≤4 SU).
    각 사이트가 자체 스파인 세트를 가지며, 테넌트 클러스터는 단일 사이트
    안에서만 구성된다(M4 배치가 보장)."""
    s = STORE
    ib = spec.IB_FABRIC

    def _su_view(su) -> dict:
        bp = spec.BLUEPRINTS.get(su.blueprint_key)
        rails = bp.connectx_supernics_per_tray if bp else 8
        racks = []
        for rack in s.racks_of_su(su.id):
            gpus = sum(len(t.gpu_ids) for t in s.trays_of_rack(rack.id))
            racks.append({"rack_id": rack.id, "tenant_id": rack.tenant_id,
                          "trays": len(rack.tray_ids), "gpus": gpus})
        trays = sum(r["trays"] for r in racks)
        return {"su_id": su.id, "blueprint_key": su.blueprint_key,
                "rails": rails,
                "rails_per_network": max(1, rails // len(ib["networks"])),
                "leaves_per_network": ib["leaves_per_network_per_su"],
                "links_800g": trays * rails,      # 16랙 SU = 2,304 (RD)
                "leaf_group": f"leaf-{su.id} (rail x{rails})",
                "racks": racks}

    with s.lock:
        sites = []
        for f in s.factories.values():
            su_ids = [sid for bid in f.block_ids
                      for did in s.blocks[bid].du_ids
                      for sid in s.dus[did].su_ids]
            sus = [_su_view(s.sus[sid]) for sid in su_ids if sid in s.sus]
            sus.sort(key=lambda v: s.sus[v["su_id"]].index)
            tag = f.site.split()[-1] if f.site else f.id
            n_su = len(sus)
            sites.append({
                "factory_id": f.id, "name": f.name, "site": f.site,
                "ib_tier": ("2-tier" if n_su <= ib["tier2_max_su"]
                            else "3-tier"),
                "converged_tier": (
                    "2-tier" if n_su <= spec.CONVERGED_FABRIC["tier2_max_su"]
                    else "3-tier"),
                "ufm_ha_sets": len(ib["networks"]),   # 서브넷별 UFM(HA)
                "networks": [
                    {"name": net,
                     "spines": [{"id": f"{tag}-{net.lower()}-core-{i + 1}",
                                 "model": "Quantum XDR"}
                                for i in range(CORE_SPINES)]}
                    for net in ib["networks"]],
                "sus": sus,
            })

        su_site = {su["su_id"]: st["name"] for st in sites for su in st["sus"]}
        all_sus = [su for st in sites for su in st["sus"]]
        tenants = []
        for t in s.tenants.values():
            t_racks = [r for su in all_sus for r in su["racks"]
                       if r["tenant_id"] == t.id]
            if not t_racks:
                continue
            ni = s.netiso.get(t.id)
            t_sus = sorted({r["rack_id"].rsplit("-rack-", 1)[0]
                            for r in t_racks})
            tenants.append({
                "tenant_id": t.id, "name": t.name,
                "pkey": hex(ni.ib_pkey) if (ni and ni.ib_pkey) else None,
                "racks": len(t_racks),
                "gpus": sum(r["gpus"] for r in t_racks),
                "sus": t_sus,
                "site": " / ".join(sorted({su_site.get(su, "?")
                                           for su in t_sus})),
            })

        out = {"sites": sites, "tenants": tenants}
        if tenant_id:
            out["selected"] = next(
                (t for t in tenants if t["tenant_id"] == tenant_id), None)
        return out
