output "repository_urls" {
  description = "ECR repository URL of every lambda."
  value = {
    health_check = module.auth_service_health_check.repository_url
    restore_db   = module.auth_service_restore_db.repository_url
    region_check = module.auth_service_region_check.repository_url
  }
}

output "deploy_role_arn" {
  description = "Value for the AWS_ROLE_ARN secret in GitHub."
  value       = aws_iam_role.deploy.arn
}

output "plan_role_arn" {
  description = "Value for the AWS_PLAN_ROLE_ARN secret in GitHub."
  value       = aws_iam_role.plan.arn
}

output "apply_role_arn" {
  description = "Value for the AWS_APPLY_ROLE_ARN secret in GitHub."
  value       = aws_iam_role.apply.arn
}