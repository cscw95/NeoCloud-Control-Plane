# End-to-End Runbook — NeoCloud OS Stack

How to bring up the whole NeoCloud OS service chain locally and verify it,
from the physical digital twin up to the customer/business/operations consoles.

```
Business/Customer/Ops consoles (:8090)          NOCP portals (:8000 /ops /customer /biz /flow ...)
        │  REST (NC.api, mock fallback)                 │
        ▼                                               ▼
NeoCloud OS Control-Plane — NOCP (:8000)  ── NOCP_NICO_URL ──►  NICo Emulator (:9000, /nico-bridge)
   fulfillment saga · Managed K8s · billing                    site controllers (gasan/ansan) · jobs
                                                                │  AI_INFRA_URL
                                                                ▼
                                                       AI Infra Emulator (:9100)
                                                       VR NVL72 physical twin · DPU isolation
                                                       fabric · telemetry (140 racks / 10,080 GPU)
```

## Repositories

| Component | Repo | Port | Role |
|---|---|---|---|
| AI Infra Emulator | <https://github.com/cscw95/AI-Infra-Emulator> | **:9100** | Physical plane — VR NVL72 rack twin, DPU-enforced tenant isolation, fabric, telemetry |
| NICo Emulator | <https://github.com/cscw95/NICo-Emulator> | **:9000** | Site-local control plane (gasan/ansan controllers, host lifecycle jobs, segments) + `/nico-bridge` for NOCP |
| NOCP (this repo) | <https://github.com/cscw95/NeoCloud-Control-Plane> | **:8000** | NeoCloud OS Control-Plane — order fulfillment saga, Managed K8s option, tenancy/isolation, billing + built-in portals |
| NeoCloud Console | <https://github.com/cscw95/NeoCloud-Console> | **:8090** | Customer / Operations / Business SPA consoles (live NOCP binding, mock fallback). Ops sidebar carries the **Managed K8S (MOCK)** group rendered inline |

All state is **in-memory** — restarting a service resets it (re-stage demo data
afterwards, see below).

## Mode A — single process (quickest)

NOCP alone, with the in-process FakeNico adapter. Good for API/portal demos
and all pytest suites.

```bash
git clone https://github.com/cscw95/NeoCloud-Control-Plane.git nocp
cd nocp && ./run.sh                    # http://127.0.0.1:8000
```

Optionally add the consoles:

```bash
git clone https://github.com/cscw95/NeoCloud-Console.git neocloud-consoles
cd neocloud-consoles && bash run.sh    # http://127.0.0.1:8090/{customer,ops,biz}/
```

## Mode B — full chain (physical twin)

Start order matters: physical plane → site control plane → NOCP → consoles.

```bash
# ① AI Infra Emulator — physical twin (:9100)
git clone https://github.com/cscw95/AI-Infra-Emulator.git ai-infra-emulator
cd ai-infra-emulator && bash run.sh                        # AI_INFRA_PORT to override

# ② NICo Emulator — site control plane (:9000); drives ① via AI_INFRA_URL (default :9100)
git clone https://github.com/cscw95/NICo-Emulator.git nico-emulator
cd nico-emulator && bash run.sh

# ③ NOCP (:8000) — real HTTP adapter against ②'s bridge
cd nocp && NOCP_NICO_URL=http://127.0.0.1:9000/nico-bridge ./run.sh
#   (topology probes use NICO_EMULATOR_URL / AI_INFRA_URL, defaults :9000 / :9100)

# ④ Consoles (:8090)
cd neocloud-consoles && bash run.sh
```

Without `NOCP_NICO_URL`, NOCP still runs (Mode A adapter) and the consoles fall
back to their built-in mocks — every layer degrades gracefully.

## Reset the whole stack (cross-tier consistency)

Every tier keeps independent in-memory state, so tenants can drift apart
(e.g. the AI Infra DPU view showing tenants the ops console no longer has).
The verification console's **전체 초기화** button — or the API below — resets
all tiers in one cascade (NOCP reseed → NICo Emulator `?cascade=true` →
AI Infra twin), and per-system resets are available too:

```bash
curl -sX POST http://127.0.0.1:8000/api/v1/admin/reset-all        # all tiers
curl -sX POST http://127.0.0.1:8000/api/v1/admin/reset/nico       # :9000 only
curl -sX POST http://127.0.0.1:8000/api/v1/admin/reset/ai-infra   # :9100 only
curl -s     http://127.0.0.1:8000/api/v1/integration/consistency  # tenant drift report
```

`/flow` also carries a **데이터 정합성** panel comparing the tenant sets of
NOCP / NICo Emulator / AI Infra and flagging orphans. Note: in Mode A the
physical tiers never see NOCP orders by design — run Mode B for full-chain
consistency (Managed K8s CP nodes work over the bridge as well).

## Stage demo data (after any NOCP restart)

```bash
B=http://127.0.0.1:8000/api/v1
# tenant delivered with the Managed K8s option (installs K8s after BMaaS acceptance)
curl -sX POST $B/tenants -H 'content-type: application/json' \
  -d '{"name":"acme-ai","isolation_tier":"bare_metal_dedicated"}'
curl -sX POST $B/orders -H 'content-type: application/json' \
  -d '{"tenant_id":"tnt-acme-ai","kind":"new","blueprint_key":"vr-nvl72","racks":1,"managed_k8s":true,"k8s_version":"v1.32.4"}'

# approval-mode order — walk the 8 fulfillment gates (incl. k8s_installing) in the ops portal
curl -sX POST $B/tenants -d '{"name":"fin-corp","isolation_tier":"bare_metal_dedicated"}' -H 'content-type: application/json'
curl -sX POST $B/orders -H 'content-type: application/json' \
  -d '{"tenant_id":"tnt-fin-corp","kind":"new","blueprint_key":"vr-nvl72","racks":1,"managed_k8s":true,"k8s_version":"v1.33.2","approval_mode":true}'

# BMaaS-only tenant — demo the Day-2 "Install Managed K8s" button in the customer console
curl -sX POST $B/tenants -d '{"name":"hyni-lab","isolation_tier":"bare_metal_dedicated"}' -H 'content-type: application/json'
curl -sX POST $B/orders -H 'content-type: application/json' \
  -d '{"tenant_id":"tnt-hyni-lab","kind":"new","blueprint_key":"vr-nvl72","racks":1}'
```

## Where to look

| URL | What |
|---|---|
| `:8090/ops/` → **Managed K8S** group (below Unified Observability, MOCK badge) | Operator-portal mockup rendered inline (no iframe) — 10 menus |
| `:8090/customer/` → clusters | Managed K8s cluster card, Day-2 install button, kubeconfig (OIDC) in the security screen |
| `:8090/biz/` → pipeline → 계약 전환 | Managed K8s option on deal conversion |
| `:8000/flow` | Verification console — pipeline state machine, **Managed K8s install verification** panel (cluster, add-ons, burn-in checks, kubeadm log; trace channels `K8s`/`rshim`) |
| `:8000/ops` | NOCP ops portal — fulfillment approval gates (k8s_installing chip) |
| `:9000/` | NICo Emulator dashboard — site controllers, orchestration state |
| `:9100/` | AI Infra Emulator dashboard — rack twin, DPU isolation |
| `:8090/ops/managed-k8s.html` | Standalone view of the Managed K8S mockup (source for `ops/build-mk8s-inline.py`) |

## Verify

```bash
# unit/regression suites
cd nocp && .venv/bin/python -m pytest tests/ -q          # 87 passed (incl. test_managed_k8s.py)
cd nico-emulator && ~/nocp/.venv/bin/python -m pytest tests/ -q   # 22 passed

# NOCP's real adapter against the emulator chain (both emulators up)
cd nocp && PYTHONPATH=. .venv/bin/python scripts/integrate_emulator.py   # 7 PASS

# full-chain proof: consoles → NOCP → NICo bridge → AI Infra twin (all four up)
cd nocp && PYTHONPATH=. .venv/bin/python scripts/e2e_full_chain.py       # 12 PASS

# scripted 13-act BMaaS demo (Mode A)
cd nocp && PYTHONPATH=. .venv/bin/python scripts/demo_scenario.py
```

## Managed K8s quick reference

- Order option: `"managed_k8s": true, "k8s_version": "v1.32.4"|"v1.33.2"` —
  inserts a `k8s_installing` stage after acceptance (8 approval gates).
- Day-2 add-on onto a delivered allocation:
  `POST /api/v1/k8s/installs {tenant_id, allocation_id, k8s_version}`.
- Read models: `GET /api/v1/k8s/clusters`, `GET /api/v1/k8s/spec`,
  `GET /api/v1/cpu-nodes` (role `k8s_cp` = 3 control-plane nodes provisioned
  through NICo's DPU path and attached to the tenant VPC on the Converged
  Network).
- DCGM telemetry flips to in-band while the cluster runs:
  `GET /api/v1/emu/faults?tenant_id=...` → `dcgm_source`.
