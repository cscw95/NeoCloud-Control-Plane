# NeoCloud Vendor Managed Kubernetes Product Research & Analysis

> Date: 2026-07-14 · Research method: web research of each vendor's official docs/product pages
> Purpose: derive supplementary items to reflect into the NOCP (NeoCloud OS Control-Plane) mockup UI and fulfillment pipeline
>
> **Our system premise**: install K8s after BMaaS provisioning, 3 CPU nodes = K8s control plane, Converged Ethernet (RoCE) network, DCGM in-band agent collection. Mockup UI 10 menus = Overview / Requests & Jobs / Clusters / Networking / Access Management / Upgrades / Incident Management / Storage / Monitoring / Settings.

---

## 1. Per-Vendor Summary

### 1.1 CoreWeave — CKS (CoreWeave Kubernetes Service)

- **Bare-metal K8s**: runs K8s directly on GPU nodes without a hypervisor. VPC orchestration and network handling are **offloaded to the BlueField-3 DPU**. The CNI is **Cilium** by default (a managed configuration with a minimal feature set).
- **Control plane**: fully managed by CoreWeave (management components run out-of-band, outside the customer's nodes). Provides customers with **control plane audit log/log access**.
- **Node lifecycle**: managed from provisioning through RMA via a Fleet/Node Lifecycle Controller + "Mission Control" (their internal system). Active health checks include **hourly HPC Verification on idle nodes (about 20 minutes, uses all GPUs, low priority and preemptible)**, and during workloads it passively monitors in-band + out-of-band telemetry. On failure, automatic remediation runs → if it fails, the node is removed from production and a **replacement node is automatically brought in**; a returning node must re-pass onboarding tests over up to 48 hours. The InfiniBand fabric is also automatically verified several times a day + manually inspected weekly.
- **Version policy**: **only sequential minor upgrades are allowed** (1.32→1.33), downgrades are not possible, and eligibility is exposed via the `isUpgradable` field.
- **Access & security**: CKS Managed Auth — **issues a kubeconfig with an embedded API Access Token** from the console, token permissions = RBAC mapping, external IdP integration (OIDC Workload Identity Federation, launched 2025-11: removes stored credentials and auto-rotates tokens).
- **GPU stack**: the GPU Operator and DCGM are pre-installed and managed by CoreWeave (customers must not install the GPU Operator themselves). DCGM chip-level metrics are exposed via a metrics query service/dashboard.
- **Observability**: **managed Grafana** (curated dashboards for node logs, GPU utilization, Kueue metrics, etc.) + also allows customers' own exporters in parallel.
- **Scheduling**: **SUNK (Slurm on Kubernetes)** — runs slurmd as pods, bidirectionally syncs Slurm↔K8s state, and lets training (Slurm) and inference (K8s) coexist on the same cluster. Provides a Kueue dashboard.
- Sources: [CKS overview](https://docs.coreweave.com/docs/products/cks) · [Node Lifecycle Day2+](https://docs.coreweave.com/docs/platform/fleet-management/node-lifecycle/day2) · [SUNK](https://docs.coreweave.com/docs/products/sunk) · [Managed Grafana](https://docs.coreweave.com/docs/observability/managed-grafana) · [API Access Token/Kubeconfig](https://docs.coreweave.com/docs/products/cks/auth-access/manage-api-access-tokens) · [Upgrade Kubernetes](https://docs.coreweave.com/docs/products/cks/clusters/upgrade-kubernetes) · [product page](https://www.coreweave.com/products/coreweave-kubernetes-service)

### 1.2 Nebius — Managed Service for Kubernetes

- **Control plane**: fully managed and **free**. HA is enabled by default (3-way etcd replication, and whether HA is used has no effect on pricing). A public endpoint by default + optional IP allowlist or private-only configuration.
- **SLA**: **99.90% availability** (downtime is counted when control plane access is unavailable for 5 minutes or more). If missed, 10%/25% credits. **However, the SLA applies only to HA (etcd 3–5) configurations** — non-HA is excluded from the SLA.
- **Node management (key differentiator)**: **auto-repair is declaratively configured per node group based on node conditions** — an automatic recovery trigger is formed by combining a condition (Ready, MemoryPressure, etc.), a status (TRUE/FALSE/UNKNOWN), and a **timeout (e.g., 2h45m)**. The update strategy is also controlled via **drain timeout / max surge / max unavailable** parameters (drain timeout defaults to 10m starting 2026 Q3).
- **GPU**: GPU node group presets (H100/H200/L40S, etc.), **NVIDIA drivers and InfiniBand drivers pre-installed**, CUDA version selection via `drivers_preset` (cuda12/cuda12.8), and a model that **attaches a node group to an existing Compute GPUCluster (InfiniBand fabric)**. There is also an NVLink instance-group attachment field.
- **Version**: default/recommended 1.33, node group version specified as `<major>.<minor>` (defaults to following the control plane version).
- **Scheduling**: **Managed Soperator** (Slurm-on-K8s) as a separate product + a K8s-native job scheduler image catalog.
- Sources: [product page](https://nebius.com/services/managed-kubernetes) · [SLA](https://docs.nebius.com/legal/sla-levels/managed-kubernetes) · [Terraform node_group reference (auto-repair)](https://docs.nebius.com/terraform-provider/reference/resources/mk8s_v1_node_group) · [cluster management](https://docs.nebius.com/kubernetes/clusters/manage) · [Managed Soperator](https://nebius.com/services/soperator)

### 1.3 Crusoe — CMK (Crusoe Managed Kubernetes)

- **Control plane**: fully managed, with **a minimum of 3 nodes distributed across different physical hosts** (HA). $0.10/h per cluster. Supports UI, CLI, API, and Terraform.
- **Node pools**: instances of the same instance type are grouped into a node pool. Changing a node pool template is **a rolling replacement rather than an in-place upgrade** (delete the node VM → self-heal with a new VM reflecting the template). Supported versions can be queried with `crusoe kubernetes versions list`.
- **Add-on catalog**: at cluster creation, install the **NVIDIA GPU Operator, NVIDIA Network Operator, Crusoe CSI, and cluster_autoscaler as selectable add-ons**.
- **Fault handling**: **AutoClusters** — automatically detects and resolves common hardware failures to minimize manual intervention (opt-in).
- **Networking**: RDMA/InfiniBand standard provided on GPU nodes (Quantum-2), with support for both NVIDIA and AMD (MI300X/MI355X). **Officially provides customers with a guide/procedure for running NCCL tests** (self-service acceptance verification).
- **Scheduling**: **Crusoe Managed Slurm on CMK** (provides Slurm as a managed offering on top of CMK), plus a Run:ai deployment guide.
- **Observability**: operates the Command Center (GPU observability + orchestration) as a separate product.
- Sources: [CMK Overview](https://docs.crusoecloud.com/orchestration/cmk/overview/) · [Node Pool management](https://docs.crusoecloud.com/orchestration/cmk/managing-nodepools) · [Slurm on CMK blog](https://www.crusoe.ai/resources/blog/slurm-on-crusoe-managed-kubernetes-how-we-built-managed-gpu-training-infrastructure) · [NCCL test guide](https://support.crusoecloud.com/hc/en-us/articles/36499606523291-Run-NCCL-Tests-On-Crusoe-Managed-Kubernetes-CMK-Cluster) · [Command Center](https://www.crusoe.ai/cloud/management-observability)

### 1.4 Lambda — Managed Kubernetes (MK8s on 1-Click Clusters)

- **Positioning**: a model that layers MK8s on top of a 1-Click Cluster (16–2,000+ GPUs, dedicated to a single customer). Lambda **handles K8s installation/upgrades, control plane HA/maintenance, node failure detection/repair, and metrics collection 24/7 on the customer's behalf**.
- **Distribution**: RKE2-based (e.g., `v1.32.3+rke2r1`).
- **GPU/network**: GPU Operator + Network Operator pre-configured. RDMA is used from pods by requesting the `rdma/rdma_shared_device_a` resource + the `IPC_LOCK` capability (InfiniBand, SHARP supported).
- **Auto-recovery**: an **auto-remediation system** that detects and recovers from hardware issues + **continuous validation** of GPU, network, and storage functionality.
- **Access**: `kubelogin` (OIDC) browser login — authenticate with a Lambda Cloud account and **automatically map account roles → ClusterRole** (Member=edit, Admin=cluster-admin); for non-interactive use, a ServiceAccount kubeconfig.
- **Observability**: an **NVIDIA DCGM Exporter Dashboard** is provided by default on a per-cluster Grafana endpoint (`grafana.<zone>.k8s.lambda.ai`).
- **Storage**: 6 StorageClasses — `lambda-shared`/`lambda-nfs` (shared), `lambda-local` (local NVMe), each with a `-retain` variant.
- Sources: [MK8s Docs](https://docs.lambda.ai/managed-kubernetes/) · [product page](https://lambda.ai/kubernetes) · [1-Click Clusters](https://lambda.ai/1-click-clusters)

### 1.5 Together AI — Instant Clusters (K8s flavor)

- **Self-service provisioning**: **self-service creation in minutes** of clusters from 8 GPUs (1 node) to hundreds of GPUs. Choose **Kubernetes or Slurm** as the cluster flavor, with SSH when needed.
- **Automation contract surface**: all management operations — create, scale, delete, storage, etc. — are provided via **REST API + Terraform**; "cluster as API" is the key differentiator.
- **Pre-configuration**: GPU Operator, Network Operator, InfiniBand, and version pinning (driver/CUDA pinning) ensure reproducibility. DC-local high-bandwidth shared storage (durable, resizable, on-demand billing).
- Sources: [Instant Clusters Docs](https://docs.together.ai/docs/instant-clusters) · [GA blog](https://www.together.ai/blog/together-instant-clusters-ga)

### 1.6 Voltage Park — Managed Kubernetes

- A **managed control plane** on top of a bare-metal GPU cluster (offloading installation, security patching, and monitoring). Launched 2025-06.
- Pre-configuration: **NVIDIA GPU Operator + Prometheus/Grafana + SentinelOne** (security observability and threat detection — notable for including a security stack by default).
- Currently targets bare-metal; VM environment support is on the roadmap.
- Sources: [product page](https://www.voltagepark.com/managed-kubernetes) · [launch press release](https://www.businesswire.com/news/home/20250604080012/en/Voltage-Park-Addresses-Kubernetes-Complexity-for-AI-Developers-with-New-Managed-Offering)

### 1.7 (Reference) NVIDIA — DGX Cloud / Mission Control (BCM) / K8s stack

- **Mission Control's K8s deployment**: installed via BCM 11's `cm-kubernetes-setup`. Configures **k8s-admin (for administration) + a workload cluster, i.e., 2 clusters**, with the control plane as **a minimum of 3 nodes = also serving as 3 etcd nodes**, K8s v1.32, CNI **Calico**, and the Prometheus Operator stack/Loki/Promtail/ingress-nginx pre-installed. From NMC 2.3, the cluster naming convention is fixed in preparation for "future automation tools" — **there is no public documentation yet under the name "NKD"** (the BCM path is the officially documented path). 2.3 adds a virtualized control plane option and unified authentication.
- **DGX Cloud Create (Run:ai on DGX Cloud)**: an NVIDIA-optimized K8s cluster on top of a CSP + a **Run:ai SaaS control plane**. Responsibilities are split within the cluster into an **NVIDIA namespace (NVIDIA-operated) / customer namespace (customer-operated)**, with a Realm (tenant) assigned per customer. The GPU cluster itself is provisioned and operated by NVIDIA.
- **GPU Operator**: manages the driver, device plugin, Container Toolkit, GFD (automatic node labeling), **DCGM exporter (in-band metrics)**, and MIG manager together as an operator — in effect adopted by default across essentially all researched vendors.
- **Network Operator**: manages K8s networking such as the RDMA shared device plugin, SR-IOV, and GPUDirect RDMA via CRDs — applicable to both RoCE and IB.
- Sources: [NMC Kubernetes Installation](https://docs.nvidia.com/mission-control/docs/nmc-software-installation-guide/2.0.0/nmc-kubernetes.html) · [NMC 2.3 Release Notes](https://docs.nvidia.com/mission-control/docs/systems-quick-start-guide/2.3.0/nmc-release-notes.html) · [Run:ai on DGX Cloud Overview](https://docs.nvidia.com/dgx-cloud/run-ai/latest/overview.html) · [GPU Operator](https://docs.nvidia.com/datacenter/cloud-native/gpu-operator/latest/index.html) · [Network Operator](https://docs.nvidia.com/networking/display/kubernetes2570/deployment-guide-kubernetes.html)

---

## 2. Feature Comparison Matrix

| Item | CoreWeave CKS | Nebius MK8s | Crusoe CMK | Lambda MK8s | Together Instant | Voltage Park | NVIDIA NMC/DGX Cloud (ref.) |
|---|---|---|---|---|---|---|---|
| Node form | **Bare-metal** (DPU isolation) | VM (GPUCluster attach) | VM node pools | Bare-metal 1CC-only | Mixed BM/VM | Bare-metal | On-prem BM |
| Control plane HA | Managed (details undisclosed), out-of-band | **etcd 3-way replication default, free** | **3 nodes / physically distributed**, $0.10/h | Managed HA included | Managed | Managed | **3 CPU nodes = also etcd** |
| Control plane SLA | Separate contract | **99.90% + credits (HA only)** | Not disclosed | Not disclosed | Not disclosed | Not disclosed | (N/A) |
| Version policy | **Sequential minor upgrades, `isUpgradable` exposed** | Default 1.33, node group follows CP version | Version list API, node pool rolling replacement | RKE2 fixed distribution, Lambda handles it | Version pinning (reproducibility) | Handled by vendor | v1.32 fixed |
| Auto-repair | **Active HPC verification on idle nodes + auto replacement + 48h re-verification on return** | **Declarative auto-repair by condition+timeout, drain/surge parameters** | AutoClusters (opt-in) | auto-remediation + continuous validation | Not disclosed | Health monitoring | NMC autonomous resiliency |
| CNI | **Cilium (eBPF)** | Not disclosed | Not disclosed | RKE2 default (Canal/Cilium) | Not disclosed | Not disclosed | **Calico** |
| RDMA | IB fat-tree + SHARP, DPU | **IB GPUCluster attach model** | IB standard + **NCCL test guide** | IB, `rdma/rdma_shared_device_a` | IB pre-configured | IB | Network Operator (SR-IOV/RDMA) |
| GPU stack | GPU Operator/DCGM **operator-managed (customer install forbidden)** | Drivers pre-installed, `drivers_preset` | GPU/Network Operator **selectable add-ons** | GPU/Network Operator pre-configured | GPU/Network Operator pre-configured | GPU Operator pre-configured | GPU Operator |
| Observability | **Managed Grafana + DCGM + control plane logs/audit** | System monitoring | Command Center | **Per-cluster Grafana + DCGM dashboard** | Not disclosed | Prometheus/Grafana + SentinelOne | Grafana dashboards (NMC) |
| kubeconfig/auth | **Token-embedded kubeconfig + OIDC federation** | Standard kubectl connection | CLI/API issuance | **kubelogin (OIDC) + role→ClusterRole mapping** | REST API | Not disclosed | NMC 2.3 unified auth |
| Storage | Managed CSI | CSI | Crusoe CSI add-on | **6 StorageClasses (local NVMe/shared/retain)** | DC-local shared storage | Not disclosed | (separate) |
| Slurm integration | **SUNK** | **Managed Soperator** | **Managed Slurm on CMK** | (separate 1CC Slurm) | Slurm flavor | None | Run:ai + Slurm |
| API/IaC | CKS API | **Official Terraform provider** | UI/CLI/API/TF | Docs-centric | **REST API + TF** | Not disclosed | BCM CLI/API |

**Key takeaways**: (1) The de facto standard configuration for GPU-cloud Managed K8s is "a managed HA control plane + GPU Operator/Network Operator pre-installed + DCGM dashboard + a Slurm-on-K8s option." (2) The axes of differentiation are the **depth of node lifecycle automation** (CoreWeave is best-in-class), the **declarative parameterization of auto-repair/upgrades** (Nebius), and **self-service API-ification** (Together). (3) Access management is shifting from "static kubeconfig download" to "**OIDC/short-lived tokens + automatic console-role→RBAC mapping**."

---

## 3. Top 8 Supplementary Recommendations

Notation: **[UI]** = add to the mockup UI's 10 menus, **[BE]** = add to the control plane backend (fulfillment pipeline).

### 1. Node lifecycle state machine + active idle-node verification (Active Health Check) — [BE]-centric, [UI] Incident Management
- **Why**: CoreWeave's core differentiator. It runs HPC Verification hourly on idle nodes (preemptible, low priority), and requires up to 48h of re-verification when a failed node is auto-replaced and returns. Lambda also explicitly cites "continuous validation." Rather than a simple "failure occurs → ticket," a **pipeline that proactively detects failures** is the foundation of product trustworthiness.
- **What**:
  - [BE] Extend the NodeInstance state machine: `provisioning → verifying(burn-in) → in_service → suspect → triage → repairing/RMA → re_verifying(48h) → in_service`. Insert a **burn-in verification job** (DCGM diag, NCCL loopback) into the acceptance stage of the existing M1/M3 saga, and schedule a preemptible verification job into idle slots during in_service.
  - [UI] Add a per-node lifecycle timeline view (state-transition history + verification results + RMA ticket links) to the Incident Management menu.

### 2. Declarative auto-repair/drain policy parameters — [BE] + [UI] Clusters
- **Why**: Nebius exposes declarative auto-repair as node condition (Ready, etc.) × status × timeout (e.g., `2h45m`) and **drain timeout / max surge / max unavailable** as part of the node group spec. Crusoe's AutoClusters is also opt-in. Since the "acceptable range for automatic replacement" differs per customer, **exposing the policy as parameters** is becoming the standard for managed products.
- **What**:
  - [BE] Add `auto_repair {condition, status, timeout, enabled}` and `update_strategy {drain_timeout, max_surge, max_unavailable}` fields to the node pool spec. A reconcile loop linking automatic drain → BMaaS node replacement → return to pool (reusing the existing NICo reconcile pattern).
  - [UI] Add an auto-repair toggle/timeout, rolling-policy sliders, and a "recent auto-recovery history" card to the node pool detail in the Clusters menu.

### 3. Control plane SLA specification & status exposure — [UI] Overview + [BE]
- **Why**: Nebius documents a 99.90% SLA, **applies the SLA only to HA (etcd 3–5) configurations**, and defines 5 minutes of unavailable control plane access as downtime. Crusoe explicitly cites "3 physically-distributed nodes." Since we already use 3 CPU nodes as the control plane, there is ample basis to **elevate the SLA to a product spec**.
- **What**:
  - [BE] A health probe for control plane components (API server/etcd) + an availability aggregation (monthly uptime %) API. Expose etcd 3-way replication status (quorum, physical distribution).
  - [UI] A "Control Plane Status / This Month's Availability / SLA 99.9%" widget on the Overview, and SLA tier/credit policy notation in the Settings menu.

### 4. OIDC/short-lived-token kubeconfig issuance + role→RBAC auto-mapping + audit log — [UI] Access Management + [BE]
- **Why**: CoreWeave evolved from console-issued token-embedded kubeconfigs to OIDC Workload Identity Federation (removing static credentials, auto-rotating), and Lambda's kubelogin browser login **automatically maps portal roles (Member=edit, Admin=cluster-admin) to ClusterRoles**. CoreWeave also opens the control plane audit log to customers.
- **What**:
  - [BE] A kubeconfig issuance API integrated with tenant IAM (short-lived tokens, with TTL/scope specification), a tenant-role→K8s RBAC mapping table, and an API server audit log collection / per-tenant filtering endpoint.
  - [UI] "Issue kubeconfig (select TTL)," a per-user role↔ClusterRole mapping table, and an audit log query tab in the Access Management menu.

### 5. Managed add-on catalog (GPU Operator / Network Operator / CSI / autoscaler / Slurm) — [UI] Clusters/Upgrades + [BE]
- **Why**: Crusoe provides the GPU Operator, Network Operator, CSI, and cluster_autoscaler as **selectable add-ons** at cluster creation, and Together guarantees reproducibility via driver/CUDA version pinning. CoreWeave, conversely, locks the GPU Operator as operator-exclusive (customer install forbidden). Either way, "**who manages the add-ons, and at what version**" is part of the product definition.
- **What**:
  - [BE] Add an **add-on installation stage** after the K8s installation step of the fulfillment pipeline: `gpu-operator (version pin, DCGM exporter included) → network-operator (RoCE/SR-IOV profile) → CSI → (optional) autoscaler/Slurm operator`. Record each add-on's version/status as a ServiceOrder artifact.
  - [UI] Add-on cards (installed / version / managed-by = operator) in the Clusters menu, and an "add-on version channel" section — separate from the "K8s version" — in the Upgrades menu.

### 6. K8s version lifecycle / sequential upgrade UX — [UI] Upgrades + [BE]
- **Why**: CoreWeave codified "one-step-at-a-time sequential minor upgrades, no downgrades, `isUpgradable` field exposed," and Crusoe upgrades node pools via **rolling replacement (node recreation)** rather than in-place. Nebius has the node group version follow the control plane version. Codifying the upgrade policy is a customer-trust factor.
- **What**:
  - [BE] `version / is_upgradable / supported_versions[]` fields on the cluster resource, and a 2-step upgrade orchestration of control plane first → node pool rolling (surge-based, using the parameters from #2), implemented as a saga with compensation on failure.
  - [UI] A version timeline (current / end-of-support scheduled / recommended), a "control plane first → node pool" 2-step progress indicator, and a sequential-upgrade constraint notice banner in the Upgrades menu.

### 7. RDMA (Converged Ethernet) verification self-service + network resource visibility — [UI] Networking + [BE]
- **Why**: Crusoe provides an NCCL test procedure as an official guide, and CoreWeave verifies the fabric automatically several times a day. Lambda even documents the RDMA resource name (`rdma/rdma_shared_device_a`) to use from pods. Since we use **Converged Ethernet (RoCE)** rather than IB, proving "NCCL performance validated even on RoCE" is actually an even more important sales point.
- **What**:
  - [BE] Include Network Operator-based RoCE profile (SR-IOV/host network, rdma shared device) installation in the add-on stage (#5) + a **customer-triggered NCCL all-reduce verification job API** (result: busbw, % vs. expected). Include passing an NCCL baseline in provisioning acceptance.
  - [UI] Per-node RDMA resource status (allocatable/in-use), a rail/leaf topology view, and a "Run NCCL Test" button with a history graph in the Networking menu.

### 8. Customer-facing observability package: managed Grafana + DCGM dashboard + control plane logs — [UI] Monitoring + [BE]
- **Why**: From CoreWeave (managed Grafana + DCGM chip-level metrics + Kueue dashboard + control plane logs) to Lambda (a DCGM Exporter Dashboard included by default in per-cluster Grafana) to Voltage Park (Prometheus/Grafana by default) — **customer exposure of DCGM-based GPU dashboards** is already the industry baseline. Since we already collect DCGM in-band agent data, we only need to add the "customer exposure layer."
- **What**:
  - [BE] A metrics query API that separates the DCGM metrics being collected into tenant scope (per-tenant label filtering), and a tenant-facing stream of K8s events and control plane logs. (Design integrated with the roadmapped NVSentinel integration.)
  - [UI] A standard GPU utilization/temperature/Xid error/ECC count dashboard (embedded) in the Monitoring menu, a "remote_write to my Prometheus" integration setting, and a cross-link with the Incident Management menu (metric anomaly → node lifecycle history).

### Prioritization Recommendation

| Rank | Item | Rationale |
|---|---|---|
| 1 | #5 add-on catalog | The backbone of the fulfillment pipeline — a prerequisite for other items (#7, #8) |
| 2 | #1 node lifecycle | The essence of GPUaaS trustworthiness; low implementation cost as an extension of the existing NodeInstance state machine |
| 3 | #8 observability package | Best ROI since DCGM collection already exists, with high demo impact |
| 4 | #4 access management | Essential for domestic enterprise/public-sector targets with security requirements |
| 5~8 | #2, #6, #3, #7 | Reflect sequentially in line with the product's maturity stage |
