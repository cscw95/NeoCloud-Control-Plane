# NeoCloud OS Control-Plane 

A **control-plane MVP** for NeoCloud (GPUaaS). It manages a multi-generation GPU cluster
(DSX AI Factory) composed of NVIDIA **NVL72** systems (**GB200 / GB300 / Vera
Rubin**). Two NVIDIA reference documents are codified into its data model and APIs.

### Supported Generations (MGX)

| Blueprint | Model | GPU/CPU | HBM | NVLink | Rack TDP | Cooling |
|---|---|---|---|---|---|---|
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
| **ComputeAdapter by NICo** | ✅ Implemented | ComputeAdapter contract (Local/HTTP implementations), NICo Day 0/1/2 simulator (job polling, fault injection) — swap only the adapter to integrate with a real NICo |
| Health & Telemetry | ✅ DSX baseline | OOB monitoring (Redfish EventService), RMA workflow, NVSentinel → NVKE node replace |
| Power & Cooling Integration | ✅ DSX baseline | MaxLPS (DPS proxy, WPPS, simulate, DSX Flex), Exchange-style event bus (SSE) |
| Billing & Metering | ✅ DSX baseline | GPU-hour, instance-hour, K8s node-hour metering |

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the detailed design and roadmap.

## End-to-End Stack

NOCP is one tier of a four-tier local stack — consoles (:8090) → **NOCP (:8000)**
→ [NICo Emulator](https://github.com/cscw95/NICo-Emulator) (:9000, `/nico-bridge`)
→ [AI Infra Emulator](https://github.com/cscw95/AI-Infra-Emulator) (:9100, VR NVL72
physical twin). See **[docs/E2E_RUNBOOK.md](docs/E2E_RUNBOOK.md)** for the full
bring-up order, demo staging and verification commands
(`scripts/e2e_full_chain.py` — 12 PASS across the chain).

## DSX OS Baseline (2026-11)

NOCP is aligned to **NVIDIA DSX OS (2026.05)** and its four layers:
Provisioning (NICo REST adapter, BMaaS pipeline, firmware) · Runtime (NVKE managed
K8s, AICR recipe) · Workload (metering) · Fleet Health (OOB, RMA, NVSentinel → node
replace). Contract: `../PLAN.md` §C.

### DSX OS baseline version matrix

Same values in every repo and in `GET /api/v1/platform/versions`:

| Component | Version |
|---|---|
| DSX OS | 2026.05 |
| NICo (infra-controller) | 2.3.0 |
| NVKE | 0.23.1 |
| DPS (MaxLPS) | 0.10 |
| Mission Control | 2.5 |
| DOCA | 3.x |
| GPU Operator | AICR recipe |
| Kubernetes | 1.33, 1.34 |
| Platform | Vera Rubin NVL72 |

### NICo adapter

| Env | Effect |
|---|---|
| `NOCP_NICO_URL` | NICo emulator root (or the old `/nico-bridge` base). Unset = in-process FakeNico |
| `NOCP_NICO_API=rest\|bridge` | `rest` (default): host reads use `GET /v2/org/{org}/nico/machine[/{id}]` (`lifecycleState` → host state, SKU from `controllerMachineType`); `bridge`: legacy `/nico-bridge` only |
| `NOCP_NICO_ORG` | `{org}` path segment, default `neocloud` |

The order pipeline's host-state steps (reserve, provision job, allocate, release,
sanitize, segments) stay on the deprecated `/nico-bridge`, which shares state with
the REST surface: a REST Instance needs a tenant VPC and gets its own id, and REST
reports reserved/provisioned/allocated all as `assigned` (re-read on the bridge).
Path table: `app/adapters.py` `NICO_REST_PATHS`; contract:
`NICo-Emulator/docs/nico-rest-paths.md`.

### New endpoints

| Area | Method · Path | Notes |
|---|---|---|
| Platform | `GET /api/v1/platform/versions` | Matrix + layers + live emulator versions |
| Catalog | `GET /api/v1/catalog` | `bmaas`, `managed-k8s` (engine `nvke`) |
| NVKE | `GET /api/v1/nvke/releases` | Releases (k8s 1.33/1.34), AICR components |
| NVKE | `GET, POST /api/v1/nvke/clusters` · `GET, PATCH, DELETE /api/v1/nvke/clusters/{id}` | List includes legacy NKD clusters (`source: nkd`, read-only here) |
| NVKE | `GET, POST /api/v1/nvke/clusters/{id}/nodepools` · `GET, PATCH(count), DELETE …/nodepools/{np}` | Each node = NICo instance (reserve → provision → allocate); GPU pools claim a whole NVL72 rack for the tenant |
| NVKE | `GET /api/v1/nvke/clusters/{id}/kubeconfig[?format=yaml]` | **Owning tenant only** |
| NVKE | `POST /api/v1/nvke/clusters/{id}/upgrade` · `GET …/aicr-recipe` · `POST …/nodes/{node}/replace` | Rolling upgrade; NVSentinel node replacement |
| MaxLPS | `GET, POST /api/v1/power/domains` · `GET, PATCH /api/v1/power/domains/{id}` | Proxy to AI-Infra DPS + ComputeBlock `power_mw` budget check (422 above budget) |
| MaxLPS | `GET /api/v1/power/profiles` · `POST /api/v1/power/simulate` · `GET, POST /api/v1/power/flex-events` · `GET /api/v1/power/telemetry?domain=` | WPPS, static vs dynamic, DSX Flex |
| MaxLPS | `POST /api/v1/racks/{rack}/power-policy` `{policy, profile?}` | Adds `dps-dynamic` and WPPS (`max-p`, `max-q`, `training`, `inference`, `balanced`; also accepted as `policy`). Cap pushed to AI-Infra (DPS rack profile + Redfish PowerLimit) |
| OOB | `GET /api/v1/oob/summary?site=` · `GET /api/v1/oob/racks/{id}` · `GET /api/v1/oob/sel/{machine}` | BMC/PSU/leak/inlet/SEL/power per rack |
| OOB | `POST /api/v1/oob/events` (Redfish EventService webhook) · `GET /api/v1/oob/events` · `POST /api/v1/oob/subscribe` | Auto-subscribes on startup (`NOCP_OOB_AUTOSUBSCRIBE=1`) |
| OOB | `POST /api/v1/oob/machines/{m}/actions/{ac-power-cycle\|gpu-reset}` | NICo `PATCH /machine/{id}/power ACPowercycle`, `POST /machine/{id}/gpu/reset`; Redfish fallback |
| Machines | `GET /api/v1/machines` · `GET /api/v1/machines/{id}` · `…/hardware` · `…/sanitize-report` | NICo REST Machine view |
| Infra proxy | `GET, POST, PATCH, PUT, DELETE /api/v1/infra/{AI-Infra path}` | Allow-list: `emulator/v1/`, `ufm/v1/`, `netq/v1/`, `vast/v1/`, `converged/v1/`, `redfish/v1/`, `dps/v1/`, `exchange/v1/topics` |
| NCP | `GET /api/v1/metering/usage?tenant=` | GPU-hour, instance-hour, K8s node-hour |
| NCP | `GET /api/v1/isolation/{tenant}` | VPC/VRF, IB P_Key, NVLink, Spectrum-X, NSG + uniqueness check |
| NCP | `GET, POST /api/v1/rma` · `GET /api/v1/rma/{id}` · `POST /api/v1/rma/{id}/advance[?to=]` | `health_event → cordon_drain → isolate → replace → burn_in → pool_return` |
| NCP | `GET /api/v1/firmware/baseline` · `GET /api/v1/firmware/drift` | NICo ext when up, else local |
| Events | `GET /api/v1/events` (SSE) · `GET /api/v1/events/recent` · `GET /api/v1/events/topics` | DSX Exchange topics `power, thermal, leak, grid, health, rma, firmware` |
| Auth | `POST /api/v1/auth/tokens` · `POST /api/v1/events/token` · `GET /api/v1/auth/whoami` | See Security |

Upstream-backed views answer with `"source": "ai-infra" | "nico"`, or
`"local-fallback"` when the emulator is down.

### Security

| Env | Default | Effect |
|---|---|---|
| `NOCP_TOKENS` | — | Static bearer tokens: `tok=operator,tok2=tenant:tnt-a[:viewer]` or JSON |
| `NOCP_TOKEN_SECRET` | random per process | HMAC key for `nocp1.*` tokens from `POST /api/v1/auth/tokens` (operator only) |
| `NOCP_DEV_INSECURE_HEADERS` | `0` | `1` = trust `X-Tenant-Id` / `X-Tenant-Role` / `X-Operator` (tests, local demo) |
| `NOCP_DEV_OPERATOR_DEFAULT` | `0` | `1` = no identity means operator (tests, `run.sh`) |
| `NOCP_CORS_ORIGINS` | `http://localhost:8090,http://127.0.0.1:8090` | Allowed console origins |
| `NOCP_WEBHOOK_SOURCES` | `127.0.0.1,::1,testclient` | IPs/CIDRs allowed to POST `/api/v1/oob/events` |
| `NOCP_WEBHOOK_SECRET` | — | When set, the webhook requires `X-Webhook-Token` |
| `NOCP_EMU_API_KEY` | — | Sent as `X-API-Key` on every NOCP → NICo / AI-Infra call (set it to the emulators' `EMU_API_KEY`) |
| `NOCP_WEBHOOK_DESTINATIONS` | `http://127.0.0.1:8000/api/v1/oob/events,http://localhost:8000/api/v1/oob/events` | Allowed EventService destinations |

- No identity on `/api/*` → 401. Without `NOCP_DEV_INSECURE_HEADERS=1`, client identity headers are stripped and replaced by the token's identity.
- SSE: `EventSource` cannot set headers, so `GET /api/v1/events?token=` takes a 5-minute signed token from `POST /api/v1/events/token`. `?operator=true` is not accepted.
- Tenants get 403 on operator surfaces (`oob`, `infra`, `power`, `rma`, `firmware`, `machines`, `events`, `admin`) and on resources owned by other tenants (tenants, orders, k8s/NVKE clusters, tickets, nodes, isolation). Lists are filtered to the caller's tenant.
- Kubeconfigs (NVKE and legacy `/k8s/.../kubeconfigs`) are released only to the owning tenant. Listings never include the body, and it is never written to the trace log.
- `/api/v1/infra/*` rejects `..`, encoded separators, `//`, `@`, and absolute URLs, and checks that the target host is AI-Infra.

`run.sh` sets both dev switches to `1` so the bundled static pages keep working.
Production leaves them `0`, and clients send `Authorization: Bearer`.

### DSX tests & smoke

```bash
.venv/bin/python -m pytest -q                                      # 194 tests
NOCP_NICO_URL=http://127.0.0.1:9000 .venv/bin/python scripts/dsx_smoke.py   # live NICo chain
.venv/bin/python scripts/e2e_dsx_chain.py      # all 4 services up: every DSX flow live (51 checks)
# secure mode (NOCP without dev switches, NOCP_TOKENS, emulators with EMU_API_KEY): 59 checks
E2E_SECURE=1 E2E_OPERATOR_TOKEN=… E2E_TENANT_TOKEN=… E2E_OTHER_TOKEN=… E2E_EMU_API_KEY=… \
  .venv/bin/python scripts/e2e_dsx_chain.py
```

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
pytest -q                                        # 194 tests (including the E2E scenario and DSX suites)
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
