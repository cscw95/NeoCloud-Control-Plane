# Managed K8s — Live 모드 아키텍처·기동 절차

Ops 콘솔 Managed K8S 화면(Mock/Live 이중 모드)의 Live 백엔드. 콘솔 쪽 구조는
`neocloud-consoles/README.md`의 "Managed K8S — Mock/Live dual mode" 참조.

## 기동 순서 (4계층)

```bash
# ① AI Infra Emulator (:9100) — VR NVL72 물리 트윈
cd ~/ai-infra-emulator && ./run.sh
# ② NICo Emulator (:9000) — 사이트 컨트롤 플레인
cd ~/nico-emulator && AI_INFRA_URL=http://127.0.0.1:9100 ./run.sh
# ③ NOCP (:8000) — NICo http 어댑터 활성화가 Live 모드의 전제
cd ~/nocp && NOCP_NICO_URL=http://127.0.0.1:9000/nico-bridge \
             AI_INFRA_URL=http://127.0.0.1:9100 ./run.sh
# ④ 콘솔 (:8090)
cd ~/neocloud-consoles && bash run.sh
```

데모 스테이징·검증: `scripts/e2e_full_chain.py` (12체크). 이후 Ops 콘솔
사이드바 Managed K8S 헤더에서 Live 전환.

## 설치 saga (R4 — NVIDIA DGX OS + NKD 25.06 정렬)

`_install_k8s`(app/lifecycle.py)가 8단계로 진행하며 단계·진행률을
`K8sCluster.stage / stage_history / progress_pct`로 노출한다:

```
cp-reserve → os-provision → net-attach → nkd-bootstrap
→ addons → acceptance → telemetry → active
```

- 페이싱: `NOCP_K8S_STAGE_DELAY` 초/단계. 기본값은 `NOCP_NICO_URL` 설정 시
  2.5(백그라운드 스레드 — 콘솔이 2~3s 폴링으로 관찰), 미설정(pytest·
  FakeNico) 시 0(동기, 기존 동작 보존).
- Day-1: `POST /api/v1/orders` + `managed_k8s:true` — 주문이 `k8s_installing`
  게이트에서 즉시 반환되고 스레드가 delivered까지 진행.
- Day-2: `POST /api/v1/k8s/installs {tenant_id, allocation_id, k8s_version}` —
  delivered 할당 위에 K8s만 얹는다(신규 BMaaS 프로비저닝 없음 — CP 3노드는
  NOCP CPU 풀에서 할당, GPU 워커는 기존 트레이 재사용).
- 실패 시 `_rollback_k8s_cp` 보상. 전 단계 trace가
  `/api/v1/orders/{id}/flow`의 k8s_installing 버킷에 기록된다.

## 콘솔 소비 API (app/k8s_api.py)

| 엔드포인트 | 내용 |
|---|---|
| `GET /api/v1/k8s/overview` | 상태별 집계·진행 중 설치/업그레이드·열린 헬스이벤트 |
| `GET /api/v1/k8s/installs` | Day-1/2 공통 설치 job + 8단계 stages |
| `GET …/clusters/{id}/nodes` | CP/GPU 워커, Ready·Draining·Quarantined |
| `GET …/acceptance` | node-ready · nccl-allreduce · dcgm-diag · storage-mount |
| `GET …/nodepools` · `GET/POST …/addons` | cp/gpu 풀 · 관리형+선택 애드온(kueue 등) |
| `GET …/services` | kube-vip API VIP + fake LB/DNS/Ingress |
| `GET/POST/DELETE …/kubeconfigs` · `GET /k8s/rbac-templates` | 가짜 PKI 시리얼·TTL·회수 |
| `POST/GET …/upgrades` · `GET /k8s/cves` | N/N-1 롤링(cordon→drain→upgrade→uncordon) |
| `GET …/health-events` | AI Infra obs/faults → 노드 매핑, quarantine·hot-spare |
| `GET …/storage` | VAST 뷰 → PVC(GDS) |
| `GET …/metrics` | DCGM 집계 + ticks (콘솔 모니터링 차트) |
| `POST /api/v1/emu/faults` | XID 주입 → 워커 Quarantined → TTL 자동 복구 |

콘솔 계약의 `active`는 내부 `running`을 매핑해 노출한다. 스토어는 전부
in-memory이며 `/api/v1/admin/reset-all` cascade에 포함된다.

## 시나리오 (검증 완료)

1. **fault → quarantine**: `POST /emu/faults {tray_id, xid:79}` → NVSentinel
   뷰에 XID 카드·quarantine·hot-spare 제안 → TTL 후 Ready 복귀.
2. **롤링 업그레이드**: v1.32.4 → v1.33.2, 147노드(CP 우선) 실이벤트 로그
   라이브 관찰, 완료 후 버전 전이·CVE 영향 소멸.

테스트: `tests/test_managed_k8s_live.py` 포함 전체 107 passed.
