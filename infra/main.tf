# One module call per lambda in lambdas/<service>/<name>/. Each one creates the ECR repository
# `<ecr_prefix>/<service>/<name>` that the lambda CD pushes its image to.
#
# The functions themselves (execution role, log group, environment, permissions) are defined in the
# infra repository (coika-disaster_recovery-infra), which looks these repositories up. Order of a
# new lambda: 1) apply this stack, 2) merge the code so the CD pushes the first image,
# 3) apply the infra repository, which creates the function from that image.

module "auth_service_restore_db" {
  source = "./modules/ecr_repository"

  ecr_prefix = var.ecr_prefix
  service    = "auth_service"
  name       = "restore_db"
}

module "auth_service_backup_db" {
  source = "./modules/ecr_repository"

  ecr_prefix = var.ecr_prefix
  service    = "auth_service"
  name       = "backup_db"
}

module "auth_service_region_check" {
  source = "./modules/ecr_repository"

  ecr_prefix = var.ecr_prefix
  service    = "auth_service"
  name       = "region_check"
}

module "auth_service_primary_recovery_check" {
  source = "./modules/ecr_repository"

  ecr_prefix = var.ecr_prefix
  service    = "auth_service"
  name       = "primary_recovery_check"
}

module "auth_service_health_check" {
  source = "./modules/ecr_repository"

  ecr_prefix = var.ecr_prefix
  service    = "auth_service"
  name       = "health_check"
}