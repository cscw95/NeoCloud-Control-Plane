# US-05 (F01/F02): a token-API key and a TPM/RPM quota; call /v1 with any OpenAI SDK.
# Generated HCL twin: main.tf (tfemu.py hcl).

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
  default = "tf-us05"
}

variable "tpm" {
  type    = number
  default = 200
}

variable "rpm" {
  type    = number
  default = 2
}

provider "neocloud" {
  base_url = var.base_url
  operator = true
}

resource "neocloud_tenant" "dev" {
  name           = var.tenant_name
  isolation_tier = "k8s_namespace"
}

resource "neocloud_token_key" "app" {
  tenant_id = neocloud_tenant.dev.id
  name      = "tf-app"
}

resource "neocloud_token_quota" "interactive" {
  tenant_id = neocloud_tenant.dev.id
  tier      = "interactive"
  tpm       = var.tpm
  rpm       = var.rpm
}

output "openai_base_url" {
  value = "${var.base_url}/v1"
}

output "api_key" {
  value     = neocloud_token_key.app.key
  sensitive = true
}

output "quota_id" {
  value = neocloud_token_quota.interactive.id
}
