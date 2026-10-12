# One-off adoption of the ECR repositories that were created by the infra repository
# (coika-disaster_recovery-infra), so that moving them here does not destroy and recreate them.
#
# Order of the migration:
#   1. terraform apply here  -> imports the existing repositories into this state (no changes expected).
#   2. Apply the infra repository, whose module no longer defines the repositories (its emoved`n#      block forgets them without destroying them).
#   3. Delete this file once the import has been applied.

# ---- auth_service/restore_db
import {
  to = module.auth_service_restore_db.aws_ecr_repository.this
  id = "${var.ecr_prefix}/auth_service/restore_db"
}

import {
  to = module.auth_service_restore_db.aws_ecr_lifecycle_policy.this
  id = "${var.ecr_prefix}/auth_service/restore_db"
}

# ---- auth_service/region_check
import {
  to = module.auth_service_region_check.aws_ecr_repository.this
  id = "${var.ecr_prefix}/auth_service/region_check"
}

import {
  to = module.auth_service_region_check.aws_ecr_lifecycle_policy.this
  id = "${var.ecr_prefix}/auth_service/region_check"
}

# ---- auth_service/health_check
import {
  to = module.auth_service_health_check.aws_ecr_repository.this
  id = "${var.ecr_prefix}/auth_service/health_check"
}

import {
  to = module.auth_service_health_check.aws_ecr_lifecycle_policy.this
  id = "${var.ecr_prefix}/auth_service/health_check"
}
