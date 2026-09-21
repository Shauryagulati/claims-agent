# Pinned so that "terraform init" resolves the same way on any machine.
# The exact provider hashes land in .terraform.lock.hcl, which is committed.

terraform {
  required_version = ">= 1.16"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}
