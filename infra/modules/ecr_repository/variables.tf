variable "ecr_prefix" {
  description = "Prefix of the ECR repository name (slashes allowed). Must match ECR_REPOSITORY_PREFIX in GitHub."
  type        = string
}

variable "service" {
  description = "Service the lambda belongs to (second level of lambdas/<service>/<name>)."
  type        = string
}

variable "name" {
  description = "Lambda name (folder name in lambdas/<service>/<name>)."
  type        = string
}

variable "images_to_keep" {
  description = "Number of images kept in the repository."
  type        = number
  default     = 10
}