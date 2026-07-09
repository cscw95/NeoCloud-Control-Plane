# NeoCloud Control-Plane — Integration API Reference

> An integration reference covering **all 98 endpoints**, extracted from the running
> server's OpenAPI schema (`GET /openapi.json`) and organized by the Control-Plane
> module structure (①–⑦, cp-*).
> Interactive documentation: http://127.0.0.1:8000/docs (Swagger UI).
>
> Legend — **[Real]**: Northbound API exposed as-is in a real deployment ·
> **[Emu]**: simulator stand-in API (mirrors the API shape of the real system; adapter swap point) ·
> **[Demo]**: verification-console only (fault injection, etc.; removed in a real deployment)

---

## A. Northbound / Public APIs (cp-api) — `/api/v1/*`

The official integration surface consumed by the three portals and external systems.
In a real deployment it is protected by OIDC Bearer (SEC01) + RBAC (SEC04).

### A-1. Service-Order Intake · Tenant Fulfillment (cp-intake / cp-fulfill)

| Method | Path | Description |
|---|---|---|
| POST | `/api/v1/orders` | **[Real]** Create an order — `kind: new` (provisioning/expansion) / `terminate` (reclamation). Body: `tenant_id, blueprint_key, racks, approval_mode, storage_mode(auto\|manual), storage_tb, storage_gbps, allocation_id` |
| GET | `/api/v1/orders` | **[Real]** List orders (`?tenant_id=` filter) |
| GET | `/api/v1/orders/{id}` | **[Real]** Order detail — includes state-machine history and the `access_package` (delivery access/credential information; secret exposed only once) |
| POST | `/api/v1/orders/{id}/approve` | **[Real]** Approval gate — executes exactly one next pipeline stage (Operations Portal) |
| POST | `/api/v1/orders/{id}/reject` | **[Real]** Reject — saga compensation rolls back any completed progress |
| GET | `/api/v1/orders/{id}/flow` | **[Real]** Full bucketing of underlying API calls per stage (for audit and the verification console) |

### A-2. Resource & Service Model (cp-model · M3 inventory/mirror)

| Method | Path | Description |
|---|---|---|
| GET | `/api/v1/inventory/summary` | **[Real]** Capacity, power, and per-generation aggregates (CAP01) |
| GET | `/api/v1/topology/tree` | **[Real]** Factory ▸ Block (floor) ▸ DU ▸ SU ▸ Rack tree (+ `power_mw`, `ready`) |
| GET | `/api/v1/factories` · `/scalable-units` · `/scalable-units/{id}` | **[Real]** Site/SU lookup |
| POST | `/api/v1/scalable-units` | **[Real]** Provision an additional SU (`?blueprint_key=`) |
| GET | `/api/v1/racks` · `/racks/{id}` · `/racks/{id}/gpus` · `/trays/{id}` | **[Real]** Rack/tray/GPU detail |
| POST | `/api/v1/racks/{id}/power-policy` | **[Real]** MaxQ/MaxP power policy |
| GET | `/api/v1/nodes` · `/nodes/{id}` · `/nodes/summary` | **[Real]** NodeInstance mirror (`?state=&tenant_id=`) — CAP02 |
| GET | `/api/v1/cpu-nodes` | **[Real]** CPU node pool (5 per tenant, DPU + VPC) — DMS01/02 |
| GET | `/api/v1/blueprints` · `/spec` | **[Real]** Generation catalog (GB200/GB300/VR) and hardware constants |
| POST | `/api/v1/reconcile/run` | **[Real]** SoT consistency audit — detects and repairs GHOST/ORPHAN/STATE_MISMATCH |

### A-3. Policy Orchestration · Tenancy/Isolation (cp-policy)

| Method | Path | Description |
|---|---|---|
| POST / GET | `/api/v1/tenants` · `/tenants/{id}` | **[Real]** Create tenants (automatic IAM realm and VNI/VRF binding) and look them up |
| GET | `/api/v1/tenants/{id}/isolation` | **[Real]** 4-layer isolation verification report (acceptance gate) |
| POST / GET / DELETE | `/api/v1/allocations` | **[Real]** Manual SU/rack-set allocation and release |
| POST / GET | `/api/v1/nvlink-partitions` | **[Real]** NVLink partitions (NMX/GFM model) — NET02 |
| GET | `/api/v1/network/vni-map` | **[Real]** Tenant VNI/VRF map (SDN01) |
| GET | `/api/v1/fabric/ib` | **[Real]** IB spine-leaf topology (`?tenant_id=` → P_Key scope) — NET01 |

### A-4. Observability · Equipment Health (cp-obs — CAP05·TEL)

| Method | Path | Description |
|---|---|---|
| GET | `/api/v1/health/equipment` | **[Real]** Equipment HwState aggregates per site/floor + abnormal-equipment list (single SoT) |
| PATCH | `/api/v1/equipment/state` | **[Real]** Operator action — rack/tray/gpu × faulted/maintenance/ready |
| GET | `/api/v1/emu/clusters` · `/emu/trays` · `/emu/trays/{id}` · `/emu/status` | **[Emu]** Tray telemetry (DCGM counterpart) — real deployment: NVSentinel/DCGM streams |
| GET | `/api/v1/emu/history` | **[Emu]** Time series (global/tenant, 240 ticks) — real deployment: OTLP time-series store |
| POST | `/api/v1/emu/tick` · `/emu/clusters/{tid}/workload` | **[Demo]** Force an emulator tick / switch workload profile |
| GET / DELETE | `/api/v1/trace` | **[Emu]** Detailed system trace bus — real deployment: OTel spans / NATS `neocloud.telemetry.*` |

### A-5. Business (Business Portal backend — tickets & billing)

| Method | Path | Description |
|---|---|---|
| POST / GET | `/api/v1/tickets` | **[Real]** Create/list tickets (`?tenant_id=&status=`) |
| PATCH | `/api/v1/tickets/{id}` | **[Real]** State transitions open→in_progress→resolved + comments |
| GET | `/api/v1/billing/usage` · `/billing/rates` | **[Real]** rack-hour usage and monthly projection, demo rate card |

### A-6. System & Admin

| Method | Path | Description |
|---|---|---|
| GET | `/health` | **[Real]** Health check (measured by the SLA management plane) |
| POST | `/api/v1/admin/reseed` | **[Demo]** Full reseed (`?blueprints=` combination) |
| GET | `/` `/ops` `/customer` `/biz` `/flow` `/nico` `/arch` | The 7 web screens |

---

## B. ④ Compute Services — NICo Emulation `/fake-nico/*`

A simulator matching the shape of the real **NICo (NVIDIA/infra-controller)** API Service.
The Control-Plane never calls this API directly — it goes through the **D1 ComputeAdapter** —
so switching to a real integration only requires replacing `LocalNicoAdapter` with
`NicoHttpAdapter` (REST/gRPC).
(The exact resource paths and schemas of the real NICo must be re-verified against the NVIDIA distribution.)

### B-1. Read APIs — Real NICo Resource Counterparts

| Method | Path | Real NICo counterpart concept |
|---|---|---|
| GET | `/fake-nico/site` | Site/facility information |
| GET | `/fake-nico/hosts` · `/hosts/{id}` | Host inventory and state (external SoT) |
| GET | `/fake-nico/instance-types` | Instance type catalog |
| GET | `/fake-nico/instances` | Tenant instance (allocation) list |
| GET | `/fake-nico/jobs` · `/jobs/{id}` | Async job polling (provision/sanitize convergence) |
| GET | `/fake-nico/dhcp/leases` | DPU-DHCP lease table |
| GET | `/fake-nico/hosts/{id}/hardware` | HW serials and firmware (BFX03) |
| GET | `/fake-nico/hosts/{id}/health` | Host BMC sensors (power, temperature, coolant) |
| GET | `/fake-nico/health?tenant_ref=` | Bulk health — consumed directly by Operations Portal Observability (CAP05) |
| GET | `/fake-nico/hosts/{id}/attestation` | TPM attestation results (CNP09) |
| GET | `/fake-nico/hosts/{id}/sanitize-report` · `/sanitize-reports` | 7-stage sanitization reports (SEC21) |

### B-2. Day 0/1/2 Action APIs — Called by the D1 Adapter

| Method | Path | Pipeline usage point |
|---|---|---|
| POST | `/fake-nico/hosts/{id}/reserve` · `/unreserve` | reserved stage (counterpart of gRPC ReserveHost) |
| POST | `/fake-nico/hosts/{id}/provision` | provisioning — BMC (Redfish) → DHCP → PXE → cloud-init substep traces |
| POST | `/fake-nico/hosts/{id}/abort-provision` | saga compensation path |
| POST / DELETE | `/fake-nico/instances` · `/instances/{id}` | Tenant allocation (allocate) / reclamation (release) |
| POST | `/fake-nico/hosts/{id}/sanitize` | 7-stage sanitization on reclamation (NVMe, GPU, TPM) |
| POST | `/fake-nico/hosts/{id}/cordon` | break-fix cordon (BFX01) |
| POST / GET / DELETE | `/fake-nico/segments` | Tenant VPC segments — applied via DPU HBN (VXLAN/EVPN) |

### B-3. Demo Only (to be removed in a real deployment)

| Method | Path | Purpose |
|---|---|---|
| POST | `/fake-nico/hosts/{id}/inject` | Inject a one-shot failure on the next call (provision/sanitize) — saga verification |
| POST | `/fake-nico/hosts/ghost` | Stage a GHOST (reconcile demo) |
| DELETE | `/fake-nico/hosts/{id}` | Stage an ORPHAN |
| PATCH | `/fake-nico/hosts/{id}/state` | Stage a STATE_MISMATCH |
| PATCH | `/fake-nico/config` | Adjust job latency (polling-convergence demo) |

---

## C. ⑥ Storage Services — VAST VMS Emulation `/fake-vast/*`

Counterpart of the real **VAST VMS REST v3** (`/api/v3/views·quotas·qospolicies`). The
Control-Plane goes through the D4 StorageAdapter — swap in a VMS REST adapter for real integration.

| Method | Path | Description |
|---|---|---|
| GET | `/fake-vast/views` | **[Emu]** Tenant view list — path, capacity (quota), QoS (bandwidth/IOPS), export subnets (restricted to the tenant VRF) |

(Creation/deletion is performed internally by the adapter during the order pipeline's
storage_binding/reclaiming stages — observable on the `VAST-API` trace channel.)

---

## D. ⑦ Shared Services — IAM·Vault·PAM Emulation `/fake-shared/*`

Real-deployment counterparts: **Keycloak** (OIDC admin/token) · **Vault** (KV v2) · PAM gateway.
Maps to SEC01·SEC04·SEC07·SEC08·SEC09.

| Method | Path | Description |
|---|---|---|
| GET | `/fake-shared/iam/realms` · `/iam/realms/{tenant_id}` | **[Emu]** Tenant realm — 3 roles (RBAC), clients (MFA), SAs (per-order, secret masked) |
| POST | `/fake-shared/iam/token` | **[Emu]** OIDC client-credentials token issuance (403 for disabled clients + audit `denied`) |
| GET | `/fake-shared/secrets?tenant_ref=` | **[Emu]** Vault KV list (values masked — s3-access-key, redfish-cred) |
| POST / GET | `/fake-shared/pam/sessions` | **[Emu]** Open a privileged-access session (operator/target/reason/TTL, recorded) and list sessions |
| POST | `/fake-shared/pam/sessions/{id}/close` | **[Emu]** Close a session |
| GET | `/fake-shared/audit?tenant_ref=&limit=` | **[Emu]** Security audit trail (SEC08) — all IAM/Vault/PAM/delivery events |

---

## E. Real-Integration Swap Points — Summary

| Emulation | Real system | How to swap |
|---|---|---|
| `/fake-nico/*` (FakeNico) | NICo — NVIDIA/infra-controller (REST/gRPC; assumes K8s, PG, Temporal) | `LocalNicoAdapter` → `NicoHttpAdapter` in `adapters.py` (same contract) |
| `/fake-vast/*` (FakeVast) | VAST VMS REST v3 | Replace the D4 StorageAdapter implementation |
| `/fake-shared/*` (FakeSharedServices) | Keycloak (OIDC) · Vault (KV) · PAM | Replace `shared_services.SHARED` call sites with real clients |
| `/api/v1/trace` (TRACER) | OTel collector · NATS `neocloud.telemetry.*` | emit() call sites are the span-creation points (M8 roadmap) |
| `/api/v1/emu/*` (EMULATOR) | NVSentinel · DCGM telemetry | Keep the read API shape; swap only the source |
