# US-02 (F06): a 4-rack, 12-month reserved contract and the order that consumes it.
# Generated HCL twin: main.tf (tfemu.py hcl). Run: tfemu.py -chdir=. init && tfemu.py -chdir=. apply

terraform {
  required_providers {
    neocloud = {
      source = "neocloud/neocloud"
      version = "~> 0.1"
    }
  }
}

variable "base_url" {
  type    = string
  default = "http://127.0.0.1:8000"
}

variable "tenant_name" {
  type    = string
  default = "tf-us02"
}

variable "start_date" {
  type    = string
  default = "2026-11-01"
}

provider "neocloud" {
  base_url = var.base_url
  operator = true
}

resource "neocloud_tenant" "customer" {
  name           = var.tenant_name
  isolation_tier = "bare_metal_dedicated"
}

resource "neocloud_contract" "reserved" {
  tenant_id     = neocloud_tenant.customer.id
  product       = "bmaas"
  commitment    = "reserved"
  racks         = 4
  blueprint_key = "vr-nvl72"
  term_months   = 12
  start_date    = var.start_date
  unit_price    = 95000000
  same_ib_zone  = true
  activate      = true
}

resource "neocloud_bmaas_order" "racks" {
  tenant_id   = neocloud_tenant.customer.id
  contract_id = neocloud_contract.reserved.id
}

output "contract_id" {
  value = neocloud_contract.reserved.id
}

output "contract_end" {
  value = neocloud_contract.reserved.end_date
}

output "order_state" {
  value = neocloud_bmaas_order.racks.state
}

output "node_count" {
  value = neocloud_bmaas_order.racks.node_count
}
