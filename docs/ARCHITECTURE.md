# NOCP Architecture Design Document

> NeoCloud OS Control Plane — the NeoCloud control plane
> Audience: SKT AI Infrastructure/GPUaaS. Basis: NVIDIA DSX Facilities Design Guide v1.0 + NCP Vera Rubin NVL72 Reference Design v3.0

---

## 1. Problem Definition — "What Are We Managing?"

Synthesizing the two NVIDIA documents, the management target is a set of **hierarchical building blocks**
plus **multi-tenant operations** on top of them.
Racks span **multiple generations** (MGX Gen 1.1 = GB200/GB300, Gen 1.2 = Vera Rubin) and can be mixed within a single factory.

```
AI Factory (250 MW)
└─ Compute Block (×4)              # 6 DUs
   └─ Deployment Unit (×6)         # availability zone, 99.9%, 3~4 SUs + Power Block
      └─ Scalable Unit (×3~4)      # 14 NVL72 racks = 1,008 GPUs, HAC unit (single generation)
         └─ Rack: NVL72            # = 1 NVLink domain (72 GPUs)
            │                      #   GB200 120kW · GB300 140kW · VR 227kW
            ├─ Compute Tray (×18)  # 4 GPUs + 2 CPUs + 1 BlueField + N×ConnectX
            └─ NVLink Switch Tray (×9)
```

**Per-generation blueprints** (`spec.BLUEPRINTS`) encapsulate GPU/CPU architecture, HBM, NVLink
generation, power caps, and cooling. The SU/DU/Block hierarchy is generation-agnostic, while the number
of racks per SU differs by generation (`blueprint.racks_per_su` — for VR, per NCP RD RD-12835-001-01 v02,
**16 racks/SU = 1,152 GPUs**, 1 SU = 1 HAC (2 rows × 8 racks, HAC TDP ~3.9MW), 2 CMX racks/SU,
and dual-network IB Fabric-A/B. GB200/GB300 remain at 14 racks/SU).
`POST /scalable-units?blueprint_key=...` provisions an SU of any generation, and
inventory aggregates decompose per generation via `gpus_by_arch` and `racks_by_generation`.

On top of this, NeoCloud isolates **multiple tenants** across 4 layers (NCP RD):
Identity → Process → Compute Virtualization → **Compute Isolation (NVLink partitions)**.
The physical boundary is the **SU/HAC**, the network boundary is **VXLAN + BGP EVPN + VRF**,
and the hardware anchor is the **BlueField-4 DPU** (NIC / DPU / DPU Zero-Trust).

NOCP **codifies this entire graph into a data model** and exposes inventory queries plus
multi-tenancy allocation and isolation verification through APIs and a dashboard.

---

## 2. System Context

```
        ┌────────────────────────────────────────────────┐
        │        NeoCloud product layer (separate)       │  ← billing, self-service, SLA (roadmap)
        └───────────────────────┬────────────────────────┘
                                │ REST API
        ┌───────────────────────┴────────────────────────┐
        │                 NOCP (this MVP)                │
        │   Inventory & Topology  │  Multi-tenancy & Iso  │
        └───┬───────────────┬───────────────┬─────────────┘
  (roadmap) │               │               │ (roadmap)
   ┌────────┴───┐   ┌───────┴──────┐  ┌─────┴───────────┐
   │ NICo/BCM   │   │ GFM (NVLink) │  │ DSX Exchange    │
   │ bare-metal │   │ partition    │  │ MQTT IT-OT bus  │
   │ provision- │   │ management   │  │ (power/thermal/ │
   │ ing        │   │              │  │  leak)          │
   └────────────┘   └──────────────┘  └─────────────────┘
        │                                     │
   ┌────┴─────────────── VR NVL72 cluster ────┴────────────────┐
   │ GPU/DPU (BlueField)  ·  Spectrum-X/Converged/OOB  ·  CDU/power │
   └────────────────────────────────────────────────────────────────┘
```

This MVP abstracts the dashed-line integrations (NICo/GFM/DSX Exchange) **at the model level**.
The `store.py` interface is kept deliberately narrow so real hardware integrations can be swapped in as adapters.

---

## 3. Data Model

### 3.1 Topology (Inventory)

| Entity | Key fields | Blueprint basis |
|---|---|---|
| `AIFactory` | site, design_power_mw, blocks | 250 MW = 4 blocks |
| `ComputeBlock` | du_ids | 6 DUs/block |
| `DeploymentUnit` | su_ids | 3~4 SUs, availability zone |
| `ScalableUnit` | rack_ids(14), hac_id, cmx_racks(2) | 1,008 GPUs |
| `Rack` | model, tdp_kw(227), power_cap_kw, tray_ids(18), nvlink_switch_tray_ids(9), tenant_id | NVL72 = 1 NVLink domain |
| `ComputeTray` | gpu_ids(4), cpu_ids(2), dpu_id, connectx9(8) | tray spec |
| `RubinGPU` | hbm4_gb(288), dies(2), state, tenant_id | |
| `VeraCPU` | cores(88), mem_tb(1.44) | |
| `BlueFieldDPU` | sku(B4240V), bandwidth(800G), **mode** | isolation anchor |

IDs are human-readable slugs (`su-1/rack-03/tray-07/gpu-2`), so the topology is grep-able
and stays stable across reseeds. All constants are consolidated in `app/spec.py` (sourced from the documents).

### 3.2 Multi-tenancy

| Entity | Key fields | Meaning |
|---|---|---|
| `Tenant` | isolation_tier, sla_tier | bare_metal_dedicated / vm_multitenant / k8s_namespace |
| `NetworkIsolation` | compute_l3vni, converged_vni, oob_vni, vrf | bound automatically at tenant creation |
| `Allocation` | scope(SU/rack_set/HAC), su_id, rack_ids, dpu_mode | capacity allocation |
| `NVLinkPartition` | rack_id (domain), partition_id, tray_ids | GFM partition (compute isolation) |

---

## 4. Multi-tenancy Isolation Model (Core Logic)

### 4.1 Tier → Automatic Policy Derivation

| Tier | Physical boundary | Default DPU mode | Notes |
|---|---|---|---|
| `bare_metal_dedicated` | exclusive SU/HAC occupancy enforced | `dpu` | 409 on attempted sharing |
| `vm_multitenant` | SU sharing allowed | `dpu_zero_trust` | isolated by a DPU air gap |
| `k8s_namespace` | soft (fractional) | `dpu` | relies on network isolation |

### 4.2 Invariants Enforced at Allocation

1. **Single-tenant racks** — a rack can never be allocated to two tenants at once (409).
2. **Dedicated-tenant SU exclusivity** — `bare_metal_dedicated` is rejected if the SU already contains co-tenant racks.
3. **VNI/VRF uniqueness** — every tenant receives collision-free VNIs across the Compute/Converged/OOB fabrics plus a VRF.
4. **NVLink partition integrity** — a partition may only use (a) racks owned by the tenant, with (b) trays belonging to that rack, and (c) no tray duplicated within the same domain.

### 4.3 Isolation Verification Report

`GET /tenants/{id}/isolation` → inspects 5 layers and yields `pass/warn/fail`:

- **identity** — tenant-scoped access control (always pass)
- **physical** — whether dedicated tiers exclusively occupy their SU
- **network** — VNI/VRF uniqueness (collision check against other tenants)
- **process** — warn if vm_multitenant is not running DPU Zero-Trust
- **compute_isolation** — NVLink partitions are consistent within owned racks

`ok=true` when there is not a single `fail`. This report is the foundation for
SLA/compliance evidence (finance, public sector).

---

## 5. API Surface

### Inventory & Topology
```
GET    /api/v1/inventory/summary          # GPU/CPU/DPU/HBM4/power aggregates
GET    /api/v1/topology/tree              # nested tree (GPUs as counts)
GET    /api/v1/factories | /scalable-units | /racks | /trays/{id}
GET    /api/v1/racks/{id} | /racks/{id}/gpus
POST   /api/v1/scalable-units             # provision a new SU from a blueprint
POST   /api/v1/racks/{id}/power-policy     # MaxQ ↔ MaxP power cap
GET    /api/v1/spec                        # blueprint constants
```

### Multi-tenancy & Isolation
```
POST   /api/v1/tenants                     # create (automatic VNI/VRF)
GET    /api/v1/tenants | /tenants/{id}
POST   /api/v1/allocations                 # SU/rack_set/HAC allocation
DELETE /api/v1/allocations/{id}            # release (rolls back partitions and bindings)
POST   /api/v1/nvlink-partitions           # GFM partition
GET    /api/v1/nvlink-partitions
GET    /api/v1/network/vni-map             # fabric VNI/VRF map
GET    /api/v1/tenants/{id}/isolation      # isolation verification report
```

---

## 6. Power & Cooling Modeling (Current Coverage)

- **MaxQ/MaxP**: expressed as the rack `power_cap_kw`. MaxQ = 200kW (throughput), MaxP = 227kW (time-to-train).
  `capped_power_mw` in `inventory/summary` aggregates the actually provisioned power →
  the basis for *capacity under a fixed power budget* calculations.
- **Cooling constants** (45°C TCS, 1.5 LPM/kW, CDU 2.3MW N+1) are kept in `spec.py`.
  Real-time telemetry and Leak Response join via the DSX Exchange adapter on the roadmap (§7).

---

## 7. Roadmap — Next Domains

| Priority | Domain | Integration points |
|---|---|---|
| 1 | **Health & Telemetry** | NVSentinel (K8s-native GPU fault handling with automatic cordon/drain), DCGM, Fleet Intelligence. Add faulted transitions to `RubinGPU.state` + automatic remediation hooks |
| 2 | **Power & Cooling IT-OT** | Subscribe to DSX Exchange (MQTT) → collect rack/CDU telemetry, Coordinated Leak Response (BMS↔Cluster Manager), DSX MaxQ dynamic power caps |
| 3 | **Scheduling & Allocation** | Slurm/KAI Scheduler/Run:ai integration; actually enforce NVLink partitions via the GFM API |
| 4 | **Billing & Metering** | Per-tenant GPU-hour/power metering → billing (NeoCloud revenue layer) |
| 5 | **Provisioning backend** | Replace `store.py` with NICo (bare metal), BCM, and DPF (large-scale BlueField management) adapters |

Design principle: **adopt NVIDIA OSS for the operational core (scheduler, health), while NOCP
owns the multi-tenancy, billing, and isolation-policy layer** — the productization layer that
DSX OS leaves open is where SKT differentiates.

---

## 8. Technical Decision Record (Summary)

- **FastAPI + Pydantic v2** — OpenAPI automation, type-safe data models, alignment with SKT's existing Python environment.
- **In-memory store behind a narrow interface** — MVP simplicity. Production swaps in an RDB/CMDB (interface unchanged).
- **Blueprint-based provisioning** — seeded and runtime SUs are structurally identical (a single `provision_scalable_unit` path).
- **Human-readable deterministic IDs** — a grep-able, reproducible topology.
- **Isolation verification as a first-class feature** — invariants enforced at allocation + after-the-fact reports. Ready as evidence for regulated industries.
