# Verification Console User Guide (`/flow` · `/nico` · `/arch`)

> **`/arch` — Platform Architecture Flow**: A screen for following order flows on top of the
> NeoCloud Platform Architecture diagram (3 portals → 10 Control Plane function blocks → 4
> infrastructure domains). Select an order → `▶ Replay` for a stage-by-stage replay: at each
> stage the involved diagram blocks, domains, and arrows light up; the state machine on the left
> shows per-stage **underlying API call counts and channel distribution**, and the right side shows
> the **full API list** (NICo REST/gRPC + BMC Redfish + DHCP + PXE + cloud-init + DPU
> HBN + UFM + NMX + VAST — all of it). Individual stages can also be explored by clicking them.
> Data: `GET /api/v1/orders/{id}/flow` — traces bucketed into stage time windows
> (attributed by order_id / node host_id / untagged system events).

> **`/nico` — NICo operations dashboard**: A page for exploring the full NICo REST API surface and understanding its behavior.
> ① Site Controller services (9) and aggregates ② host inventory (state filters, search) ③ host drill-down
> (hardware / health BMC sensors / attestation TPM PCRs / sanitize) ④ instance type catalog, instances,
> Segments (VPC), Jobs, DHCP Leases ⑤ **tenant cluster compute tray emulation (LIVE, 2s tick)**
> — tray heatmap (color = GPU util, red border = fault), cluster cards (average util, power/cap, temperature, NVLink,
> tok/s, ECC, faults), workload profile switching (training/inference/idle) ⑥ tray detail (per-GPU
> util/temperature/power/HBM/ECC/XID for each of the 4 GPUs). XID faults are also recorded on the trace bus (DCGM→NVSentinel).
> ⑥ **IB fabric topology** — a rail-optimized 2-tier (4 core spines × per-SU rail leaves) SVG.
> Selecting a tenant from the full physical-resource view highlights only that P_Key partition's GPU fabric
> (rack, leaf, and spine paths). Deep link: `/nico#tenant=tnt-x`. Data: `GET /api/v1/fabric/ib`.
> Suggested order: provision in `/flow` → check the live operational state in `/nico`.
>
> **Default seed = Phase 1 30MW**: VR SU×10 + GB200 SU×1 = 154 racks / 2,772 trays /
> 11,088 GPUs ≈ 29.7MW (MaxQ). Orders may exceed a single SU (rack-count preset: 1 SU = 14) — a
> best-fit within a single SU is tried first, and any shortfall spills over across SUs, split into per-SU allocations.
> The host inventory paginates at 50 hosts/page; instances, Jobs, and Leases are full scrolling lists.

A console for verifying, step by step in the browser, the interplay between NeoCloud OS
(order pipeline, M3 mirror, reconcile) and Fake NICo (Day 0/1/2). Start the server, then open http://127.0.0.1:8000/flow.

```bash
cd ~/nocp && ./run.sh          # or: .venv/bin/python -m uvicorn app.main:app --port 8000
```

## Screen Layout

| Section | Contents |
|---|---|
| ① Module architecture | The module chain a request passes through. The currently executing stage lights up green (red on failure) |
| ② Scenario controls | Reset / tenant / provisioning / fault injection / reclamation / reconcile / job latency |
| ③ Pipeline state machine | Animated replay of order state transitions in timestamp order |
| ④⑤ Mirror views | NodeInstance (M3) vs NICo hosts — rack × tray grid, color-coded by state |
| D2 panel | Tenant isolation configuration — VPC (VRF + 3 VNIs) · NICo segments · NVLink partitions |
| D4 panel | VAST storage — view path/capacity/QoS/export restrictions (VMS control results) |
| ⑥ Manual mode | Run the Day 0/1/2 APIs one button at a time against a single host (host_ip shown) |
| ⑦ Reconcile results | GHOST/ORPHAN/MISMATCH findings (severity colors) |
| ⑧ Detailed system trace | **A complete record of internal behavior, separate from the API log** — per-channel messages for BMC (Redfish), DHCP, PXE, cloud-init, DPU (NVUE/HBN), UFM, NMX, and VAST-API. Click a row → the actual payload JSON. Supports filters (host/keyword) and a channel selector |
| ⑨ API call log | Every HTTP call made by the page. Click a row → request/response JSON |

### The Detailed Flow of a Single Activation as Seen in Trace ⑧ (~211 entries per rack)
1. `Portal/API → M1` order intake → `M4 → M1` NVL placement decision (rack-list payload)
2. Per tray: `D1 → NICo` ReserveHost (gRPC) → ProvisionHost (REST)
3. Per-tray NICo southbound control: `NICo → BMC` Redfish PXE one-shot boot + ForceRestart →
   `DPU-DHCP → Host` IP lease (/30, yiaddr payload) → `Host → NICo.PXE` image streaming →
   `NICo.PXE → Host` cloud-init (static-IP netplan, UEFI lockdown, BMC credential rotation)
4. `D1 → NICo` AllocateInstance (gRPC) → `NICo → FMDS` metadata registration
5. Isolation: `D2 → NICo` VPC (segment) creation → per tray `DPU-Agent → HBN` VRF/VXLAN/EVPN application
   → `D2 → UFM` P_Key binding → `D2 → NMX` NVLink partition
6. Storage: `D4 → VAST.VMS` view creation → CNode export activation → quota → QoS
7. Verification: all 3 IsolationVerifier checks PASS (cross-vrf / ib-pkey / nvlink)
8. Reclamation in reverse: NMX/UFM teardown → segment deletion → VAST view destruction → release → 7-stage sanitize

## Recommended Scenario Order

### 0. Initialization
`⟲ Full Reset` — reseeds tenants, orders, nodes, and NICo. Whenever state gets tangled, press this.

### 1. Normal Activation (F1 happy path)
1. Enter a tenant name → `+ Create`
2. Blueprint `vr-nvl72`, rack count `1` → `▸ Provision`
3. Observe: the 8-stage `received→…→delivered` replay in ③, module lights in ①,
   one rack (18 cells) turning `in_service` (bright green) in ④⑤, and API records in ⑧

### 2. Saga Compensation (fault injection)
1. Fault injection: keep the default target host (`nh-su-2-rack-00-tray-00`), select `provision` → `⚡ Inject`
2. `▸ Provision` — if this order is placed on that rack, it fails during provision
3. Observe: red `compensating→failed` chips in ③, only the affected node `quarantined` (red),
   everything else back to `pool_ready`, and no leftover tenant allocation
4. Re-run → placement automatically skips the unhealthy rack and reaches `delivered`

### 3. Reclamation & Sanitization (F4)
1. Select the target allocation → `▸ Reclaim`
2. Observe: drain→release→sanitize, then return to the pool. In ⑥, select that host and use
   `5-1. Sanitize report` → confirm all 7 stages (erase/wipe/TPM/re-attestation) PASS
3. RMA path: inject a `sanitize` fault before reclamation → only that node goes `rma`, and the order error explicitly calls for physical disposal

### 4. Reconcile (consistency audit)
- `Create GHOST` → registers a host that exists only in NICo → `Run Reconcile` → info, auto-registered as discovered
- `Create ORPHAN` → deletes the host from NICo → critical, node cordoned (excluded from the sellable pool)
- `Create MISMATCH` (requires an in_service node — provision first) → silently tampers with the NICo state
  → critical, **the node state is not changed automatically** (detect-and-escalate principle)

### 5. Manual Mode (Day 0/1/2 APIs step by step)
1. Set `NICo job latency` to `2` (to observe the polling behavior)
2. Enter a host (default `nh-su-1-rack-13-tray-17`) → `Get state`
3. `1. reserve` → `2. provision` (job running) → `2-1. poll job` ×2 (succeeded)
   → `3. allocate` → `4. release` → `5. sanitize` → `5-1. Sanitize report`
4. The state-machine chip moves through `pool_ready→reserved→provisioning→provisioned→allocated→released→sanitizing→pool_ready`

> Note: manual mode manipulates only NICo, so it diverges from the M3 nodes → the scenario
> ends with `Run Reconcile` catching the MISMATCH. Finish with `⟲ Full Reset`.

## Troubleshooting

| Symptom | Cause / action |
|---|---|
| Order `rejected` (insufficient capacity) | Not enough racks of that generation, or racks excluded due to unhealthy nodes — reduce the rack count or reset |
| Order `failed` (reserve failed) | Mismatch between NICo and the mirror (e.g. orphaned hosts) — a normal saga. Retrying routes placement around it |
| Manual mode 409 | Host-state precondition violated (e.g. provision without reserve) — an intentional guard |
| Page looks broken | Refresh (⌘R), then `⟲ Full Reset` |
| Stop/restart the server | `kill $(lsof -ti :8000)` / `cd ~/nocp && ./run.sh` |
