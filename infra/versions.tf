terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # Same bucket as the infra repository (DR region, reachable during a primary region outage),
  # but its own state file. Backend blocks cannot use variables.
  backend "s3" {
    bucket       = "coika-dr-tfstate-s3"
    key          = "lambdas/terraform.tfstate"
    region       = "eu-central-1"
    use_lockfile = true
    encrypt      = true
  }
}

provider "aws" {
  region = var.dr_region

  default_tags {
    tags = {
      Project   = "coika"
      ManagedBy = "terraform"
    }
  }
}
