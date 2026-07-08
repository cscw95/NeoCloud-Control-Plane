# DPU Isolation 동작 상세 — 실 NICo(NVIDIA/infra-controller) 코드 분석

> 분석 대상: `github.com/NVIDIA/infra-controller` (main, Apache-2.0 — "NVIDIA Infra
> Controller — Hardware Lifecycle Management and multitenant networking", 9,100+ 파일).
> 아래 파일 경로는 모두 이 저장소 기준이며, vrcm의 Fake NICo가 이 동작을 재현한다.

---

## 1. 구성 요소

| 구성 요소 | 실코드 위치 | 역할 |
|---|---|---|
| **API Service (코드명 carbide)** | `crates/api-core`, `crates/api-db` | VPC/network-segment desired state의 단일 기록자 (PostgreSQL) |
| **dpu-agent** | `crates/agent/` (Rust 데몬, `bluefield/misc/forge-dpu-agent.service`) | BlueField ARM 위에서 실행 — 네트워크 구성 적용의 실행 주체 |
| **HBN 컨테이너** | agent가 제어 (`crates/agent/src/hbn.rs`) | 컨테이너화된 Cumulus Linux — NVUE/FRR 데이터플레인 |
| **admin-cli** | `crates/admin-cli/src/{vpc,network_segment,vpc_peering,dpu}/` | 운영자 CLI — segment attach_vpc, virtualizer 전환, DPU reprovision |
| **FMDS** | `crates/agent/src/{fmds_client,metadata_service,instance_metadata_endpoint}.rs` | 인스턴스 메타데이터 서비스 (169.254.169.254) |

## 2. 가상화 모드 — `VpcVirtualizationType` (`crates/agent/src/nvue.rs`)

| 모드 | 의미 | 특징 |
|---|---|---|
| **FNN** (신규, 기본) | L3 EVPN 기반 | vrf당 EVPN VNI, 멀티 DPU, 인터페이스별 라우팅 프로파일, NSG, /31(v4)·/127(v6) 링크넷 |
| **ETV** (legacy) | Ethernet Virtualizer (+NVUE) | VLAN 액세스 포트 중심, 테넌트-wide 설정 병합, OVS 브리징(`ovs.rs`, `traffic_intercept_bridging.rs`) |

- 템플릿: `crates/agent/templates/nvue_startup_fnn.conf` / `nvue_startup_etv.conf`
- 전환: `admin-cli vpc set_virtualizer`

## 3. 격리 적용 흐름 (FNN 기준)

```
① 주문/할당 → API(carbide)에 VPC + network-segment 기록 (desired state)
② 각 DPU의 dpu-agent: periodic_config_fetcher가 gRPC로
   ManagedHostNetworkConfigResponse 폴링 (config_fetch_interval 주기)
   — tenant_interfaces(vlan·vni·prefix), NSG, interface_routing_profiles,
     instance metadata 포함                    (periodic_config_fetcher.rs)
③ agent가 NvueConfig 빌드(nvue.rs build()) → FNN 템플릿 렌더 → HBN 컨테이너 적용
   — nv config apply → `ifreload -a` → neighmgr 재시작,
     forge-arp-accept ifupdown2 정책 주입       (hbn.rs)
④ BGP 수렴 검증 — vtysh BGP summary 파싱: ToR peers Established,
   route server peers(l2vpn-evpn), 테넌트 경로 광고 여부
   (hbn_bgp_summary_*.json 픽스처들)            (hbn.rs, health.rs)
   실패 시 unhealthy 보고 → 재수렴 (메인 루프가 지속 리컨사일)
```

## 4. 렌더되는 NVUE 실형상 (FNN L3 — `templates/tests/full_nvue_startup_fnn_l3.yaml.expected`)

- **VRF**: 데이터플레인 VRF는 `vpc_<vni>` 명명 (예: `vpc_10101`) — EVPN `vni '10101'`,
  테넌트 앵커 loopback /32, ipv4-unicast + l2vpn-evpn BGP
- **호스트 대면 포트 `pf0hpf_if`**: 테넌트 VRF 바인딩 + **ACL 체인**
  `p0000_deny_prefixes_ipv4` → `p0004_security_policy_override_{v4,v6}_{ingress,egress}`
  → `p0010_vpc_<vni>_isolation_ipv4` → NSG 규칙 (reflexive-acl on)
- **FMDS 링크넷 `pf0dpu1_if`**: `169.254.169.253/30`을 테넌트 VRF 안에 두어
  인스턴스가 169.254.169.254 메타데이터에 접근
- **VXLAN NVE**: source lo, arp-nd-suppress on
- **언더레이 BGP**: `p0_if`/`p1_if` unnumbered eBGP(peer-group underlay) —
  리프 스위치와 직결, multipath 128
- **routeserver peer-group**: multihop-255 · update-source lo · l2vpn-evpn 전용
- **라우트맵/커뮤니티**: `dpu_to_evpn`, `leak_to_underlay`,
  BYOIP `65100:01/02` 커뮤니티 리스트, `DPU_TO_EVPN_AS_PATH_DROP_LIST`

## 5. vrcm 재현 매핑

| 실코드 동작 | vrcm 구현 |
|---|---|
| carbide VPC/segment 기록 | `nico_fake.create_segment` — internal 이벤트(virtualizer·vrf_dataplane·RT) |
| dpu-agent gRPC 폴링 | 세그먼트당 gRPC 이벤트 `GetManagedHostNetworkConfig` |
| 호스트별 NVUE 렌더→HBN 적용 | 호스트당 1개 `NVUE/HBN` 이벤트 — FNN 페이로드(vrf vpc_<vni>·pf0hpf ACL 체인·FMDS 링크넷·underlay/routeserver BGP·apply 시퀀스) |
| BGP summary 검증 | 세그먼트당 internal 이벤트(tor/routeserver/tenant_routes PASS) |
| 해체(EVPN withdraw) | `delete_segment` — NVUE 재렌더·vrf 삭제·type-5 withdraw |
| CPU 노드 VPC 연결 | `_sync_cpu_nodes` — 동일 FNN 렌더 표기 |

관측 지점: `/flow` ⑧ 트레이스(채널 NVUE/HBN·gRPC·internal, 행 클릭 → NVUE 페이로드),
D2 격리 패널(segment에 FNN·vrf_dataplane·VNI 표기), `/arch` isolating 단계 API 리스트.

> 주: IB P_Key(UFM)·NVLink 파티션(NMX)은 NICo 범위 밖의 상용 컴포넌트로, DPU
> isolation(Ethernet/VPC 계층)과 별개 축이다. 실 NICo에는 VPC peering
> (`vpc_peering/`)·BYOIP·IPv6 dual-stack도 있으나 vrcm은 아직 미재현(백로그).
