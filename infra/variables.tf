variable "dr_region" {
  description = "Disaster recovery region. The ECR repositories are created here, never in the primary region, so they survive a primary region outage."
  type        = string
}

variable "ecr_prefix" {
  description = "Prefix for ECR repositories. Same value as the ECR_REPOSITORY_PREFIX GitHub variable."
  type        = string
  default     = "coika/disaster_recovery"
}

variable "name_prefix" {
  description = "Prefix of the function names. Same value as the LAMBDA_NAME_PREFIX GitHub variable."
  type        = string
  default     = "coika"
}

variable "github_sub_prefix" {
  description = "Prefix of the OIDC `sub` claim of this GitHub repository (it uses immutable subjects: owner and repo IDs). Get it with `gh api repos/<owner>/coika-disaster_recovery/actions/oidc/customization/sub`."
  type        = string
  default     = "repo:acortescab@289379741/coika-disaster_recovery@1415542251"
}

variable "github_apply_environment" {
  description = "GitHub environment (with required reviewers) that is allowed to assume the apply role."
  type        = string
  default     = "prod"
}

variable "state_bucket" {
  description = "S3 bucket that holds the Terraform state (same as the backend in versions.tf of both stacks, which cannot use variables)."
  type        = string
  default     = "coika-dr-tfstate-s3"
}

variable "state_key" {
  description = "Key of the state of the terraform/ stack (the one the pipeline applies). The plan and apply roles can only reach this key and its lock file."
  type        = string
  default     = "lambdas/terraform.tfstate"
}

variable "create_github_oidc_provider" {
  description = "Create the GitHub OIDC provider. Leave false if the AWS account already has one (only one per account is allowed); it is then looked up."
  type        = bool
  default     = false
}
