"""In-memory data store for the NOCP MVP.

A single process-wide registry of all topology + tenancy entities, keyed by id.
Thread-safe via a coarse re-entrant lock (the MVP is read-heavy and low-QPS).

For production this maps cleanly onto:
  - topology entities  -> a graph / relational store (NICo / CMDB)
  - tenancy entities   -> the control-plane DB backing the IaaS API
The interface here is intentionally narrow so the backing store can be swapped.
"""

from __future__ import annotations

import threading
from typing import Optional, TypeVar

from .models import (
    AIFactory,
    Allocation,
    BlueFieldDPU,
    ComputeBlock,
    ComputeTray,
    CPU,
    CpuNode,
    DeploymentUnit,
    GPU,
    NetworkIsolation,
    NodeInstance,
    NVLinkPartition,
    NVLinkSwitchTray,
    Rack,
    ScalableUnit,
    ServiceOrder,
    StorageAllocation,
    Tenant,
    Ticket,
)

T = TypeVar("T")


class Store:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        # topology
        self.factories: dict[str, AIFactory] = {}
        self.blocks: dict[str, ComputeBlock] = {}
        self.dus: dict[str, DeploymentUnit] = {}
        self.sus: dict[str, ScalableUnit] = {}
        self.racks: dict[str, Rack] = {}
        self.trays: dict[str, ComputeTray] = {}
        self.nvlink_trays: dict[str, NVLinkSwitchTray] = {}
        self.gpus: dict[str, GPU] = {}
        self.cpus: dict[str, CPU] = {}
        self.dpus: dict[str, BlueFieldDPU] = {}
        # tenancy
        self.tenants: dict[str, Tenant] = {}
        self.allocations: dict[str, Allocation] = {}
        self.partitions: dict[str, NVLinkPartition] = {}
        self.netiso: dict[str, NetworkIsolation] = {}  # keyed by tenant_id
        # service lifecycle (M3/M1)
        self.node_instances: dict[str, NodeInstance] = {}
        self.orders: dict[str, ServiceOrder] = {}
        self.storage_allocs: dict[str, StorageAllocation] = {}
        self.tickets: dict[str, Ticket] = {}
        self.cpu_nodes: dict[str, CpuNode] = {}
        # monotonic counters for generated ids
        self._counters: dict[str, int] = {}

    # -- id helpers ---------------------------------------------------------
    def next_seq(self, name: str) -> int:
        with self.lock:
            n = self._counters.get(name, 0) + 1
            self._counters[name] = n
            return n

    def reset(self) -> None:
        with self.lock:
            for d in (
                self.factories, self.blocks, self.dus, self.sus, self.racks,
                self.trays, self.nvlink_trays, self.gpus, self.cpus, self.dpus,
                self.tenants, self.allocations, self.partitions, self.netiso,
                self.node_instances, self.orders, self.storage_allocs,
                self.tickets, self.cpu_nodes,
            ):
                d.clear()
            self._counters.clear()

    # -- convenience lookups ------------------------------------------------
    def factory_of_su(self, su_id: str):
        """SU가 속한 AIFactory(사이트) — 사이트 간에는 IB/NVLink 크로스가 없다."""
        for f in self.factories.values():
            for bid in f.block_ids:
                block = self.blocks.get(bid)
                if not block:
                    continue
                for did in block.du_ids:
                    du = self.dus.get(did)
                    if du and su_id in du.su_ids:
                        return f
        return None

    def racks_of_su(self, su_id: str) -> list[Rack]:
        su = self.sus.get(su_id)
        if not su:
            return []
        return [self.racks[r] for r in su.rack_ids if r in self.racks]

    def trays_of_rack(self, rack_id: str) -> list[ComputeTray]:
        rack = self.racks.get(rack_id)
        if not rack:
            return []
        return [self.trays[t] for t in rack.tray_ids if t in self.trays]

    def gpus_of_rack(self, rack_id: str) -> list[GPU]:
        out: list[GPU] = []
        for tray in self.trays_of_rack(rack_id):
            out.extend(self.gpus[g] for g in tray.gpu_ids if g in self.gpus)
        return out

    def allocations_of_tenant(self, tenant_id: str) -> list[Allocation]:
        return [a for a in self.allocations.values() if a.tenant_id == tenant_id]

    def allocation_for_rack(self, rack_id: str) -> Optional[Allocation]:
        for a in self.allocations.values():
            if rack_id in a.rack_ids:
                return a
        return None

    def partitions_of_rack(self, rack_id: str) -> list[NVLinkPartition]:
        return [p for p in self.partitions.values() if p.rack_id == rack_id]

    def nodes_of_rack(self, rack_id: str) -> list[NodeInstance]:
        return [n for n in self.node_instances.values() if n.rack_id == rack_id]

    def node_by_nico_host(self, nico_host_id: str) -> Optional[NodeInstance]:
        for n in self.node_instances.values():
            if n.nico_host_id == nico_host_id:
                return n
        return None


# process-wide singleton
STORE = Store()
