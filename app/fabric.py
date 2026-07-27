"""IB fabric topology read-model — for visualizing the spine/leaf(rail)/rack layout.

Exposes a simplified view of the NCP reference's rail-optimized 2-tier compute fabric:
  - a tray's SuperNIC port n connects to the rail-n leaf switch within the SU
    (rail count = blueprint's connectx_supernics_per_tray),
  - an SU's leaf group has a full-mesh uplink to all core(spine) switches,
  - tenant isolation is expressed via the UFM P_Key (NetworkIsolation.ib_pkey).

The /nico dashboard's SVG renderer draws this read-model as-is. The cable-level
SoT belongs to Nautobot(D3); this is a summary model for comprehension/visualization.
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
    """Per-site (AIFactory) **independent** IB fabric — no cross-connect between Gasan and Ansan.

    Compute fabric per NCP RD (RD-12835-001-01 v02):
      - dual-network Fabric-A/B (separate subnets → 2 sets of UFM(HA)),
      - 4 rails × 16 leaves/SU per network, 2,304×800G node-leaf links per SU,
      - 2-tier ≤9 SU / 3-tier 10~32 SU (converged is 2-tier ≤4 SU).
    Each site has its own spine set, and a tenant cluster is composed within a
    single site only (guaranteed by M4 placement)."""
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
                "links_800g": trays * rails,      # 16-rack SU = 2,304 (RD)
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
                "ufm_ha_sets": len(ib["networks"]),   # UFM(HA) per subnet
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
