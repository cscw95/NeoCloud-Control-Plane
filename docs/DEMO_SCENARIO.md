# BMaaS Service E2E Demo Scenario (13 Acts · A Portal Journey)

Demonstrates and verifies the full behavior as a role-based BMaaS service journey:
**Business Portal** (contracts & billing) → **Customer Portal** (self-service, SLA, tickets) → **NeoCloud OS**
(pipeline, isolation, storage) → **NICo/facilities** (emulation) → **Operations Portal** (break-fix, tickets, consistency).

```bash
cd ~/nocp && ./run.sh                                  # ① start the server (separate terminal)
.venv/bin/python scripts/demo_scenario.py              # ② automated run (33 checks, ~10s)
.venv/bin/python scripts/demo_scenario.py --pause      # ③ presentation mode — Enter between acts, follow along on screen
pytest tests/test_e2e_scenario.py -q                   # CI verification (same journey, 2-SU scale)
```

## Service Screens (7 — cross-navigate via the top-right nav)

| Screen | Role |
|---|---|
| `/ops` Operations Portal | **⚡ Fulfillment board (3 buckets: awaiting approval / delivered / failed-rejected; chevron progress track + pulse highlight)** · **Tenant Observability (direct NICo `/health` bulk-sensor integration — GPU node table, BMC power/temperature/coolant + DCGM trend charts + XID events)** · Break-fix queue · Reconcile · Ticket handling |
| `/customer` Customer Portal | Tenant scope: live cluster view · **4 service-status line charts (util / power + cap / temperature / throughput & NVLink)** · **per-rack detail** · SLA visibility · self-service · tickets · storage |
| `/biz` Business Portal | Customer & contract management · **fleet-wide GPU usage (allocation rate, util, power trend charts)** · **per-tenant detail (click a row → that customer's trend charts)** · usage/billing preview · ticket queue |
| `/` NOCP | Inventory, topology, allocations (operational detail) |
| `/flow` verification console | Pipeline replay, fault injection, reconcile staging |
| `/nico` NICo dashboard | Full NICo REST explorer, LIVE tray emulation, IB fabric |
| `/arch` architecture | Order replay on the system diagram + full list of underlying API calls per stage |

## Approval-Gated Fulfillment (Demo Highlight)

A Business Portal "contract + activation request" creates an order in `approval_mode`, which then
waits in the **Operations Portal Fulfillment queue**. The operator approves the pipeline stages
(policy/placement → reservation → provisioning → isolation → storage → acceptance testing → delivery)
**one at a time**; between gates, the "awaiting operator approval: {stage}" badge on the
Customer/Business Portals, the `/nico` host states (pool_ready→reserved→allocated),
the `/arch` flow stages and Operator approval events, and the `/flow` grid all sync in real time.
On rejection: rejected outright if nothing has started; otherwise saga compensation fully rolls back the progress.
API: `POST /orders` `{approval_mode:true}` → `POST /orders/{id}/approve|reject`.

## Acts

| Act | Journey (role) | Key checks | Screen |
|---|---|---|---|
| 0 | Initialization | 30MW reconfiguration (2,772 nodes), tickets/VAST/VPC empty, all 7 screens served | `/flow` ⟲ |
| 1 | Infrastructure verification | 29.68MW · 9 NICo services · 4 spines × 11 SUs | `/nico` |
| 2 | **Biz — contract signing** | Tenant creation → automatic VRF/VNI binding, rate card | `/biz` |
| 3 | **Customer — self-service activation** | VR 16 racks delivered (0.1s) · VAST 8,000TB · P_Key | `/customer` |
| 4 | Flow audit | 3,196 underlying calls (Redfish 576 · HBN 288 · NMX 16 …) | `/arch` |
| 5 | Operations (emulated) | training 91%/2.6MW → inference 55%/1.7MW | `/nico` LIVE |
| 6 | **Customer — SLA visibility** | Lead time 0.1s · availability 100% · isolation PASS | `/customer` SLA |
| 7 | Fault/saga | Injected failure → failed · 1 node quarantined · retry delivered | `/flow` |
| 8 | **Ticket (customer → operations)** | Filed (high) → in progress (comment) → resolved, customer confirms | `/customer`+`/ops` |
| 9 | Consistency | GHOST/ORPHAN/MISMATCH detected → repaired | `/ops` Reconcile |
| 10 | **Customer — expansion** | +2 racks, P_Key reuse | `/customer` |
| 11 | **Reclamation + Biz billing** | 7-stage sanitize · teardown in reverse order; beta line closed · acme monthly projection computed | `/biz` billing |
| 12 | Restore | Full restoration to the initial state (idempotent) | `/flow` ⟲ |

## About Real NICo Integration

The real NICo (github.com/NVIDIA/infra-controller) is a Rust+Go workspace whose runtime
assumes Kubernetes (3-node HA), PostgreSQL, Temporal, and BMC/DPU endpoints (the web UI is
embedded in the API service, OIDC). Since it cannot be started in a local demo environment,
this workspace substitutes Fake NICo for its REST surface. **Swap points for real integration**:
`LocalNicoAdapter` → `NicoHttpAdapter(base_url=the real site controller)` in `adapters.py`;
NICo's own portal is reached at that controller URL (linked from the Operations Portal).
