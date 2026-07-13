# DPU Isolation in Depth — Code Analysis of the Real NICo (NVIDIA/infra-controller)

> Analysis target: `github.com/NVIDIA/infra-controller` (main, Apache-2.0 — "NVIDIA Infra
> Controller — Hardware Lifecycle Management and multitenant networking", 9,100+ files).
> All file paths below refer to this repository; nocp's Fake NICo reproduces this behavior.

---

## 1. Components

| Component | Location in the real code | Role |
|---|---|---|
| **API Service (codename carbide)** | `crates/api-core`, `crates/api-db` | Single writer of VPC/network-segment desired state (PostgreSQL) |
| **dpu-agent** | `crates/agent/` (Rust daemon, `bluefield/misc/forge-dpu-agent.service`) | Runs on the BlueField ARM cores — the executor that applies network configuration |
| **HBN container** | controlled by the agent (`crates/agent/src/hbn.rs`) | Containerized Cumulus Linux — NVUE/FRR data plane |
| **admin-cli** | `crates/admin-cli/src/{vpc,network_segment,vpc_peering,dpu}/` | Operator CLI — segment attach_vpc, virtualizer switching, DPU reprovision |
| **FMDS** | `crates/agent/src/{fmds_client,metadata_service,instance_metadata_endpoint}.rs` | Instance metadata service (169.254.169.254) |

## 2. Virtualization Modes — `VpcVirtualizationType` (`crates/agent/src/nvue.rs`)

| Mode | Meaning | Characteristics |
|---|---|---|
| **FNN** (new, default) | L3 EVPN based | EVPN VNI per VRF, multi-DPU, per-interface routing profiles, NSGs, /31 (v4) · /127 (v6) link nets |
| **ETV** (legacy) | Ethernet Virtualizer (+NVUE) | Centered on VLAN access ports, tenant-wide config merging, OVS bridging (`ovs.rs`, `traffic_intercept_bridging.rs`) |

- Templates: `crates/agent/templates/nvue_startup_fnn.conf` / `nvue_startup_etv.conf`
- Switching: `admin-cli vpc set_virtualizer`

## 3. Isolation Application Flow (FNN)

```
① Order/allocation → VPC + network-segment recorded in the API (carbide) (desired state)
② On each DPU, the dpu-agent's periodic_config_fetcher polls
   ManagedHostNetworkConfigResponse over gRPC (every config_fetch_interval)
   — includes tenant_interfaces (vlan/vni/prefix), NSGs, interface_routing_profiles,
     and instance metadata                     (periodic_config_fetcher.rs)
③ The agent builds an NvueConfig (nvue.rs build()) → renders the FNN template → applies it to the HBN container
   — nv config apply → `ifreload -a` → neighmgr restart,
     forge-arp-accept ifupdown2 policy injection  (hbn.rs)
④ BGP convergence verification — parses vtysh BGP summary: ToR peers Established,
   route server peers (l2vpn-evpn), tenant route advertisement
   (the hbn_bgp_summary_*.json fixtures)          (hbn.rs, health.rs)
   On failure, reports unhealthy → re-convergence (the main loop reconciles continuously)
```

## 4. The NVUE Shape Actually Rendered (FNN L3 — `templates/tests/full_nvue_startup_fnn_l3.yaml.expected`)

- **VRF**: data-plane VRFs are named `vpc_<vni>` (e.g. `vpc_10101`) — EVPN `vni '10101'`,
  tenant anchor loopback /32, ipv4-unicast + l2vpn-evpn BGP
- **Host-facing port `pf0hpf_if`**: bound to the tenant VRF + an **ACL chain**
  `p0000_deny_prefixes_ipv4` → `p0004_security_policy_override_{v4,v6}_{ingress,egress}`
  → `p0010_vpc_<vni>_isolation_ipv4` → NSG rules (reflexive-acl on)
- **FMDS link net `pf0dpu1_if`**: places `169.254.169.253/30` inside the tenant VRF so
  instances can reach the 169.254.169.254 metadata endpoint
- **VXLAN NVE**: source lo, arp-nd-suppress on
- **Underlay BGP**: `p0_if`/`p1_if` unnumbered eBGP (peer-group underlay) —
  directly attached to the leaf switches, multipath 128
- **routeserver peer-group**: multihop-255 · update-source lo · l2vpn-evpn only
- **Route maps/communities**: `dpu_to_evpn`, `leak_to_underlay`,
  BYOIP `65100:01/02` community lists, `DPU_TO_EVPN_AS_PATH_DROP_LIST`

## 5. nocp Reproduction Mapping

| Behavior in the real code | nocp implementation |
|---|---|
| carbide VPC/segment recording | `nico_fake.create_segment` — internal event (virtualizer, vrf_dataplane, RT) |
| dpu-agent gRPC polling | one `GetManagedHostNetworkConfig` gRPC event per segment |
| Per-host NVUE render → HBN apply | one `NVUE/HBN` event per host — FNN payload (vrf vpc_<vni>, pf0hpf ACL chain, FMDS link net, underlay/routeserver BGP, apply sequence) |
| BGP summary verification | one internal event per segment (tor/routeserver/tenant_routes PASS) |
| Teardown (EVPN withdraw) | `delete_segment` — NVUE re-render, VRF deletion, type-5 withdraw |
| CPU node VPC attachment | `_sync_cpu_nodes` — same FNN render notation |

Observation points: the `/flow` ⑧ trace (channels NVUE/HBN, gRPC, internal; click a row → the NVUE payload),
the D2 isolation panel (segments annotated with FNN, vrf_dataplane, VNI), and the `/arch` isolating-stage API list.

> Note: IB P_Key (UFM) and NVLink partitions (NMX) are commercial components outside NICo's
> scope — a separate axis from DPU isolation (the Ethernet/VPC layer). The real NICo also has
> VPC peering (`vpc_peering/`), BYOIP, and IPv6 dual-stack, which nocp does not reproduce yet (backlog).
