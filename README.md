# NeoCloud OS Control-Plane 

A **control-plane MVP** for NeoCloud (GPUaaS). It manages a multi-generation GPU cluster
(DSX AI Factory) composed of NVIDIA **NVL72** systems (**GB200 / GB300 / Vera
Rubin**). Two NVIDIA reference documents are codified into its data model and APIs.

### Supported Generations (MGX)

| Blueprint | Model | GPU/CPU | HBM | NVLink | Rack TDP | Cooling |
|---|---|---|---|---|---|---|
| `gb200-nvl72` | GB200 NVL72 (Gen 1.1) | Blackwell / Grace | 192 GB HBM3e | NVLink5 (1.8 TB/s) | 120 kW (nominal · MaxQ not published) | hybrid |
| `gb300-nvl72` | GB300 NVL72 (Gen 1.1)¹ | Blackwell Ultra / Grace | 288 GB HBM3e | NVLink5 | 135 kW (peak ~155 · MaxQ not published) | liquid |
| `vr-nvl72` | Vera Rubin NVL72 (Gen 1.2) | Rubin / Vera | 288 GB HBM4 | NVLink6 (3.6 TB/s) | 227 kW (MaxP 227 · MaxQ 187) | liquid |

¹ GB300 power figures are public estimates (`preliminary=true`). Generations can be mixed within a single AI Factory.

> Source documents
> - *NVIDIA DSX Facilities Infrastructure Design Guide* v1.0 (2026-03-12)
> - *NVIDIA Cloud Partner: Vera Rubin NVL72 Systems Reference Design* (PRD12771-001 v3.0)

## Scope of This MVP

| Domain | Status | Description |
|---|---|---|
| **Inventory & Topology** | ✅ Implemented | AI Factory ▸ Block ▸ DU ▸ SU ▸ Rack (NVL72) ▸ Tray ▸ GPU/CPU/DPU expansion, capacity/power aggregation, blueprint-based SU provisioning, MaxQ/MaxP power policies |
| **Multi-tenancy & Isolation** | ✅ Implemented | Tenant lifecycle, capacity allocation (SU/rack-set/HAC), NVLink partitions (GFM model), automatic VNI/VRF binding, 4-layer isolation verification report |
| **Service Lifecycle (M1/M3/M4-lite)** | ✅ Implemented | Order pipeline (with saga compensation), NodeInstance/ServiceOrder state machines, NVL-domain-integrity placement, reclamation & sanitization, NICo reconcile (GHOST/ORPHAN/MISMATCH) |
| **D1 ComputeAdapter + Fake NICo** | ✅ Implemented | ComputeAdapter contract (Local/HTTP implementations), NICo Day 0/1/2 simulator (job polling, fault injection) — swap only the adapter to integrate with a real NICo |
| Health & Telemetry | 🔜 Roadmap | Unified GPU/DPU/network/facility monitoring (NVSentinel integration) |
| Power & Cooling Integration | 🔜 Roadmap | DSX Exchange (MQTT) IT-OT, CDU/TCS, Coordinated Leak Response |
| Billing & Metering | 🔜 Roadmap | Per-tenant usage metering and billing (NeoCloud revenue layer) |

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the detailed design and roadmap.

## End-to-End Stack

NOCP is one tier of a four-tier local stack — consoles (:8090) → **NOCP (:8000)**
→ [NICo Emulator](https://github.com/cscw95/NICo-Emulator) (:9000, `/nico-bridge`)
→ [AI Infra Emulator](https://github.com/cscw95/AI-Infra-Emulator) (:9100, VR NVL72
physical twin). See **[docs/E2E_RUNBOOK.md](docs/E2E_RUNBOOK.md)** for the full
bring-up order, demo staging and verification commands
(`scripts/e2e_full_chain.py` — 12 PASS across the chain).

## Quick Start

```bash
cd ~/nocp
./run.sh                      # create venv + install dependencies + start the server
```

- **Three portals**: Operations http://127.0.0.1:8000/ops · Customer /customer · Business /biz (tickets, SLA, billing preview)
- Dashboard: http://127.0.0.1:8000/
- Verification console: http://127.0.0.1:8000/flow — usage guide: [docs/FLOW_CONSOLE_GUIDE.md](docs/FLOW_CONSOLE_GUIDE.md)
- NICo operations dashboard: http://127.0.0.1:8000/nico — full REST explorer + LIVE tray emulation
- Architecture flow: http://127.0.0.1:8000/arch — order replay on top of the system diagram + full list of underlying API calls per stage
- API docs (OpenAPI): http://127.0.0.1:8000/docs
- **Integration API reference** (98 endpoints including the NICo emulation, with real-integration swap points): [docs/API_REFERENCE.md](docs/API_REFERENCE.md)
- **DPU isolation behavior in depth** (analysis of the real NICo infra-controller code + nocp mapping): [docs/DPU_ISOLATION.md](docs/DPU_ISOLATION.md)
- Health check: http://127.0.0.1:8000/health

The default seed is the **Phase 1 production deployment configuration (all Vera Rubin)** — 2 sites × 2 floors each.

Provisioning additional generations:
```bash
# Add a GB300 SU
curl -XPOST "localhost:8000/api/v1/scalable-units?blueprint_key=gb300-nvl72"
# Catalog of supported generations
curl localhost:8000/api/v1/blueprints
# Reseed with any combination of generations
curl -XPOST "localhost:8000/api/v1/admin/reseed?blueprints=gb200-nvl72,gb300-nvl72,vr-nvl72"
```

## Tests & Demo

```bash
. .venv/bin/activate
pytest -q                                        # 45 tests (including the E2E scenario)
python scripts/demo_scenario.py                  # full end-to-end demo runner (11 acts, 37 checks)
python scripts/demo_scenario.py --pause          # presentation mode
```

Demo scenario details: [docs/DEMO_SCENARIO.md](docs/DEMO_SCENARIO.md)

## Key API Examples

```bash
# Inventory summary
curl localhost:8000/api/v1/inventory/summary

# Create a tenant (automatic VNI/VRF binding)
curl -XPOST localhost:8000/api/v1/tenants \
  -H 'content-type: application/json' \
  -d '{"name":"fin-corp","isolation_tier":"vm_multitenant"}'

# Allocate an entire SU (DPU mode derived automatically from the tier)
curl -XPOST localhost:8000/api/v1/allocations \
  -H 'content-type: application/json' \
  -d '{"tenant_id":"tnt-fin-corp","su_id":"su-1","scope":"scalable_unit"}'

# NVLink partition (compute isolation)
curl -XPOST localhost:8000/api/v1/nvlink-partitions \
  -H 'content-type: application/json' \
  -d '{"rack_id":"su-1-rack-00","tenant_id":"tnt-fin-corp","tray_ids":["su-1-rack-00-tray-00"]}'

# Isolation verification report
curl localhost:8000/api/v1/tenants/tnt-fin-corp/isolation

# --- Service lifecycle (M1-lite) ---
# New provisioning order: placement -> reservation -> NICo provisioning -> isolation -> acceptance in one shot (saga rollback on failure)
curl -XPOST localhost:8000/api/v1/orders \
  -H 'content-type: application/json' \
  -d '{"tenant_id":"tnt-fin-corp","kind":"new","blueprint_key":"vr-nvl72","racks":2}'

# Managed K8s option: after BMaaS acceptance, installs K8s on the cluster —
# 3 control-plane CPU nodes provisioned via NICo (DPU isolation) and attached
# to the tenant VPC over the Converged Network; DCGM telemetry switches to
# in-band (dcgm-exporter DaemonSet). Adds a `k8s_installing` pipeline stage.
curl -XPOST localhost:8000/api/v1/orders \
  -H 'content-type: application/json' \
  -d '{"tenant_id":"tnt-fin-corp","kind":"new","blueprint_key":"vr-nvl72","racks":2,"managed_k8s":true,"k8s_version":"v1.32.4"}'

# Day-2 add-on: install Managed K8s on an already-delivered BMaaS allocation
curl -XPOST localhost:8000/api/v1/k8s/installs \
  -H 'content-type: application/json' \
  -d '{"tenant_id":"tnt-fin-corp","allocation_id":"alloc-1","k8s_version":"v1.32.4"}'

# Managed K8s clusters / product spec (versions, CP sizing, managed add-ons)
curl localhost:8000/api/v1/k8s/clusters
curl localhost:8000/api/v1/k8s/spec

# Reclamation: drain -> release -> sanitization (7 stages) -> return to pool (RMA escalation on failure)
# (tears down the Managed K8s cluster and returns CP CPU nodes first, if present)
curl -XPOST localhost:8000/api/v1/orders \
  -H 'content-type: application/json' \
  -d '{"tenant_id":"tnt-fin-corp","kind":"terminate","allocation_id":"alloc-1"}'

# Node pool status / NICo consistency audit
curl localhost:8000/api/v1/nodes/summary
curl -XPOST localhost:8000/api/v1/reconcile/run

# Direct Fake NICo manipulation (fault injection, etc.)
curl -XPOST localhost:8000/fake-nico/hosts/nh-su-2-rack-00-tray-00/inject \
  -H 'content-type: application/json' -d '{"op":"provision"}'
```

## Structure

```
nocp/
├─ app/
│  ├─ spec.py       # NVL72/DSX hardware & topology constants (single source of truth for the documents)
│  ├─ models.py     # Pydantic data models (topology + tenancy)
│  ├─ store.py      # in-memory store (replaceable interface)
│  ├─ seed.py       # blueprint-based SU provisioning + default seed
│  ├─ topology.py   # inventory & topology APIs
│  ├─ tenancy.py    # multi-tenancy & isolation APIs + verification logic
│  ├─ lifecycle.py  # order pipeline (saga), state machines, placement, reconcile (M1/M3/M4-lite)
│  ├─ adapters.py   # D1 ComputeAdapter contract + Local/HTTP implementations
│  ├─ nico_fake.py  # NICo Day 0/1/2 simulator (+ /fake-nico REST)
│  ├─ main.py       # FastAPI assembly + lifespan seeding
│  └─ static/index.html   # lightweight dashboard
├─ tests/           # pytest (topology + tenancy + lifecycle)
├─ docs/ARCHITECTURE.md   # detailed design document
├─ requirements.txt
└─ run.sh
```
