"""Emulated `neocloud/neocloud` Terraform provider (F29): schemas + CRUD over the SDK.

A real provider is a gRPC plugin (tfplugin6). This emulator keeps the parts a user
sees and that tools read: the provider schema in `terraform providers schema -json`
form (format_version 1.0), resource CRUD semantics (create / read / update /
replace on ForceNew attributes / delete), and the state file v4 written by tfemu.py.
Every call goes through the generated SDK, so it only uses public NOCP operations.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sdk"))

from neocloud_sdk import Client, NotFoundError  # noqa: E402

PROVIDER_ADDR = "registry.terraform.io/neocloud/neocloud"
PROVIDER_VERSION = "0.1.0"


def A(type_, *, required=False, optional=False, computed=False, sensitive=False,
      force_new=False, default=None, desc=""):
    return {"type": type_, "required": required, "optional": optional, "computed": computed,
            "sensitive": sensitive, "force_new": force_new, "default": default, "description": desc}


PROVIDER_SCHEMA = {
    "base_url": A("string", optional=True, desc="NOCP base URL (env NEOCLOUD_BASE_URL)"),
    "token": A("string", optional=True, sensitive=True, desc="Bearer token (env NEOCLOUD_TOKEN)"),
    "operator": A("bool", optional=True, desc="send X-Operator (dev stacks only)"),
}


# --------------------------------------------------------------------------- resources
def _tenant_create(c: Client, a: dict) -> dict:
    r = c.tenants.create_tenant({"name": a["name"], "isolation_tier": a["isolation_tier"]})
    return {**a, "id": r["id"], "status": r.get("status")}


def _tenant_read(c: Client, s: dict) -> dict | None:
    try:
        t = c.tenants.get_tenant(s["id"])["tenant"]
    except NotFoundError:
        return None
    return {**s, "status": t.get("status")}


def _tenant_delete(c: Client, s: dict) -> str:
    # NOCP has no tenant DELETE: offboarding is the termination workflow (backup, wipe).
    return "tenant kept in NOCP (offboarding is POST /api/v1/tenants/{id}/termination); removed from state"


def _contract_body(a: dict) -> dict:
    return {"tenant_id": a["tenant_id"], "product": a["product"], "commitment": a["commitment"],
            "racks": a["racks"], "blueprint_key": a["blueprint_key"], "term_months": a["term_months"],
            "start_date": a["start_date"], "unit_price": a["unit_price"],
            "placement": {"same_ib_zone": bool(a["same_ib_zone"])}, "sla_tier": a["sla_tier"]}


def _contract_create(c: Client, a: dict) -> dict:
    r = c.contracts.create_contract(_contract_body(a))
    if a.get("activate"):
        r = c.contracts.activate_contract(r["id"])
    return {**a, "id": r["id"], "status": r["status"], "end_date": r.get("end_date"),
            "currency": r.get("currency")}


def _contract_read(c: Client, s: dict) -> dict | None:
    try:
        r = c.contracts.get_contract(s["id"])
    except NotFoundError:
        return None
    return {**s, "status": r["status"], "end_date": r.get("end_date")}


def _contract_update(c: Client, s: dict, a: dict) -> dict:
    body = {k: v for k, v in _contract_body(a).items() if k not in ("tenant_id", "product", "commitment")}
    r = c.contracts.patch_contract(s["id"], body)
    return {**a, "id": s["id"], "status": r["status"], "end_date": r.get("end_date"),
            "currency": r.get("currency")}


def _contract_delete(c: Client, s: dict) -> str:
    c.contracts.patch_contract(s["id"], {"status": "terminated"})
    return "contract terminated (PATCH status=terminated)"


def _order_create(c: Client, a: dict) -> dict:
    body = {"tenant_id": a["tenant_id"], "kind": "new"}
    if a.get("contract_id"):
        body["contract_id"] = a["contract_id"]
    else:
        body.update(blueprint_key=a["blueprint_key"], racks=a["racks"])
    r = c.orders.create_order(body)
    if r.get("state") == "rejected":
        raise RuntimeError(f"order {r.get('id')} rejected: {r.get('error')}")
    return {**a, **_order_view(r)}


def _order_view(r: dict) -> dict:
    return {"id": r["id"], "state": r.get("state"), "racks": r.get("racks"),
            "blueprint_key": r.get("blueprint_key"), "node_count": len(r.get("node_ids") or []),
            "allocation_ids": list(r.get("allocation_ids") or [])}


def _order_read(c: Client, s: dict) -> dict | None:
    try:
        r = c.orders.get_order(s["id"])
    except NotFoundError:
        return None
    return {**s, **{k: v for k, v in _order_view(r).items() if k in ("state", "node_count")}}


def _order_delete(c: Client, s: dict) -> str:
    done = []
    try:   # idempotent: only release allocations the tenant still holds
        live = {a.get("id") for a in c.tenants.get_tenant(s["tenant_id"]).get("allocations") or []}
    except NotFoundError:
        live = set()
    for alloc in [a for a in s.get("allocation_ids") or [] if a in live]:
        r = c.orders.create_order({"tenant_id": s["tenant_id"], "kind": "terminate",
                                   "allocation_id": alloc})
        done.append(f"{r.get('id')}:{r.get('state')}")
    return "released " + (", ".join(done) or "nothing")


def _nvke_create(c: Client, a: dict) -> dict:
    pools = [{"name": p["name"], "instance_type": p["instance_type"], "count": p["count"]}
             for p in a.get("nodepool") or []]
    r = c.nvke.create_cluster({"name": a["name"], "tenant_id": a["tenant_id"],
                               "k8s_version": a["k8s_version"], "nodepools": pools})
    # kubeconfig is released to the owning tenant only (NOCP F11), so read it as the tenant
    owner = c.with_options(operator=False, tenant_id=a["tenant_id"]) if not c.auth_token else c
    kc = owner.nvke.kubeconfig(r["id"])
    return {**a, "id": r["id"], "status": r.get("state"), "node_count": r.get("node_count"),
            "kubeconfig": kc.get("kubeconfig") if isinstance(kc, dict) else kc}


def _nvke_read(c: Client, s: dict) -> dict | None:
    try:
        r = c.nvke.get_cluster(s["id"])
    except NotFoundError:
        return None
    return {**s, "status": r.get("state"), "node_count": r.get("node_count")}


def _nvke_delete(c: Client, s: dict) -> str:
    c.nvke.delete_cluster(s["id"])
    return "cluster deleted"


def _key_create(c: Client, a: dict) -> dict:
    r = c.token.create_key({"tenant_id": a["tenant_id"], "name": a["name"]})
    return {**a, "id": r["id"], "key": r["key"], "prefix": r.get("prefix")}


def _key_read(c: Client, s: dict) -> dict | None:
    keys = c.token.list_keys(tenant_id=s["tenant_id"])
    rows = keys.get("keys", []) if isinstance(keys, dict) else keys
    hit = [k for k in rows or [] if k.get("id") == s["id"] and not k.get("revoked")]
    return s if hit else None


def _key_delete(c: Client, s: dict) -> str:
    c.token.revoke_key(s["id"])
    return "key revoked"


def _quota_apply(c: Client, a: dict, s: dict | None = None) -> dict:
    r = c.token.put_quota(a["tenant_id"], {"tier": a["tier"], "tpm": a["tpm"], "rpm": a["rpm"]})
    return {**a, "id": f"{a['tenant_id']}/{a['tier']}", "source": r.get("source")}


def _quota_read(c: Client, s: dict) -> dict | None:
    return s


def _quota_delete(c: Client, s: dict) -> str:
    return "quota left as configured (NOCP has no quota DELETE); removed from state"


RESOURCES: dict[str, dict[str, Any]] = {
    "neocloud_tenant": {
        "description": "Customer organisation (POST /api/v1/tenants).",
        "attributes": {
            "id": A("string", computed=True),
            "name": A("string", required=True, force_new=True),
            "isolation_tier": A("string", optional=True, force_new=True, default="bare_metal_dedicated"),
            "status": A("string", computed=True),
        },
        "create": _tenant_create, "read": _tenant_read, "delete": _tenant_delete,
    },
    "neocloud_contract": {
        "description": "Reserved (committed) contract, F06 (POST /api/v1/contracts + /activate).",
        "attributes": {
            "id": A("string", computed=True),
            "tenant_id": A("string", required=True, force_new=True),
            "product": A("string", optional=True, force_new=True, default="bmaas"),
            "commitment": A("string", optional=True, force_new=True, default="reserved"),
            "racks": A("number", required=True),
            "blueprint_key": A("string", optional=True, default="vr-nvl72"),
            "term_months": A("number", required=True),
            "start_date": A("string", required=True, desc="YYYY-MM-DD"),
            "unit_price": A("number", required=True, desc="KRW per rack-month"),
            "same_ib_zone": A("bool", optional=True, default=True),
            "sla_tier": A("string", optional=True, default="standard"),
            "activate": A("bool", optional=True, force_new=True, default=True),
            "status": A("string", computed=True),
            "end_date": A("string", computed=True),
            "currency": A("string", computed=True),
        },
        "create": _contract_create, "read": _contract_read, "update": _contract_update,
        "delete": _contract_delete,
    },
    "neocloud_bmaas_order": {
        "description": "Bare-metal order (POST /api/v1/orders kind=new; destroy = kind=terminate).",
        "attributes": {
            "id": A("string", computed=True),
            "tenant_id": A("string", required=True, force_new=True),
            "contract_id": A("string", optional=True, force_new=True),
            "blueprint_key": A("string", optional=True, computed=True, force_new=True),
            "racks": A("number", optional=True, computed=True, force_new=True),
            "state": A("string", computed=True),
            "node_count": A("number", computed=True),
            "allocation_ids": A(["list", "string"], computed=True),
        },
        "create": _order_create, "read": _order_read, "delete": _order_delete,
    },
    "neocloud_nvke_cluster": {
        "description": "Managed Kubernetes cluster, F11 (POST /api/v1/nvke/clusters).",
        "attributes": {
            "id": A("string", computed=True),
            "name": A("string", required=True, force_new=True),
            "tenant_id": A("string", required=True, force_new=True),
            "k8s_version": A("string", optional=True, force_new=True, default="1.33"),
            "nodepool": A(["list", ["object", {"name": "string", "instance_type": "string",
                                               "count": "number"}]], required=True, force_new=True),
            "status": A("string", computed=True),
            "node_count": A("number", computed=True),
            "kubeconfig": A("string", computed=True, sensitive=True),
        },
        "create": _nvke_create, "read": _nvke_read, "delete": _nvke_delete,
    },
    "neocloud_token_key": {
        "description": "Token-API key, F01 (POST /api/v1/token/keys); secret only at create.",
        "attributes": {
            "id": A("string", computed=True),
            "tenant_id": A("string", required=True, force_new=True),
            "name": A("string", required=True, force_new=True),
            "key": A("string", computed=True, sensitive=True),
            "prefix": A("string", computed=True),
        },
        "create": _key_create, "read": _key_read, "delete": _key_delete,
    },
    "neocloud_token_quota": {
        "description": "TPM/RPM quota per tier, F02 (PUT /api/v1/token/quotas/{tenant_id}).",
        "attributes": {
            "id": A("string", computed=True),
            "tenant_id": A("string", required=True, force_new=True),
            "tier": A("string", optional=True, force_new=True, default="interactive"),
            "tpm": A("number", required=True),
            "rpm": A("number", required=True),
            "source": A("string", computed=True),
        },
        "create": _quota_apply, "read": _quota_read,
        "update": lambda c, s, a: _quota_apply(c, a, s), "delete": _quota_delete,
    },
}


def _block(attrs: dict) -> dict:
    out = {}
    for name, a in attrs.items():
        row = {"type": a["type"], "description_kind": "plain"}
        if a["description"]:
            row["description"] = a["description"]
        for flag in ("required", "optional", "computed", "sensitive"):
            if a[flag]:
                row[flag] = True
        out[name] = row
    return {"attributes": out, "description_kind": "plain"}


def providers_schema() -> dict:
    """`terraform providers schema -json` document for this provider."""
    return {
        "format_version": "1.0",
        "provider_schemas": {
            PROVIDER_ADDR: {
                "provider": {"version": 0, "block": _block(PROVIDER_SCHEMA)},
                "resource_schemas": {
                    t: {"version": 0, "block": {**_block(r["attributes"]),
                                                "description": r["description"]}}
                    for t, r in sorted(RESOURCES.items())
                },
                "data_source_schemas": {},
            }
        },
    }


def defaults(rtype: str, cfg: dict) -> dict:
    out = {}
    for name, a in RESOURCES[rtype]["attributes"].items():
        if name in cfg:
            out[name] = cfg[name]
        elif a["default"] is not None:
            out[name] = a["default"]
        elif not a["computed"]:
            out[name] = None
    return out


def validate(rtype: str, cfg: dict) -> list[str]:
    if rtype not in RESOURCES:
        return [f'The provider neocloud/neocloud does not support resource type "{rtype}".']
    attrs = RESOURCES[rtype]["attributes"]
    errs = [f'Unsupported argument "{k}" in {rtype}' for k in cfg
            if k not in attrs or (attrs[k]["computed"] and not attrs[k]["optional"])]
    errs += [f'The argument "{k}" is required in {rtype}' for k, a in attrs.items()
             if a["required"] and k not in cfg]
    return errs


Op = Callable[..., Any]
