# Roles that the workflows of this repository assume through GitHub OIDC:
#
#   deploy  CD of the lambdas (cd.yml): pushes the images to ECR and updates the functions.
#           Only the main branch can assume it.            -> secret AWS_ROLE_ARN
#   plan    `terraform plan` on pull requests (terraform-ci.yml). Read-only.   -> AWS_PLAN_ROLE_ARN
#   apply   `terraform apply` (terraform-cd.yml). Only the GitHub environment `prod`, whose required
#           reviewers are the approval gate.                                   -> AWS_APPLY_ROLE_ARN
#
# The apply role has no IAM write permissions, so the pipeline can never change these roles (or
# grant itself more access). The first apply, and any later change to this file, is done by hand
# from a workstation (see README); a PR that changes a role fails at apply with AccessDenied.

data "aws_caller_identity" "current" {}

resource "aws_iam_openid_connect_provider" "github" {
  count = var.create_github_oidc_provider ? 1 : 0

  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
}

data "aws_iam_openid_connect_provider" "github" {
  count = var.create_github_oidc_provider ? 0 : 1

  url = "https://token.actions.githubusercontent.com"
}

locals {
  account_id               = data.aws_caller_identity.current.account_id
  github_oidc_provider_arn = var.create_github_oidc_provider ? aws_iam_openid_connect_provider.github[0].arn : data.aws_iam_openid_connect_provider.github[0].arn

  state_object_arn = "arn:aws:s3:::${var.state_bucket}/${var.state_key}"
}

# Trust policy of a role: GitHub OIDC with the given `sub` claim.
data "aws_iam_policy_document" "assume" {
  for_each = {
    deploy = "${var.github_sub_prefix}:ref:refs/heads/main"
    plan   = "${var.github_sub_prefix}:pull_request"
    apply  = "${var.github_sub_prefix}:environment:${var.github_apply_environment}"
  }

  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [local.github_oidc_provider_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = [each.value]
    }
  }
}

# ------------------------------------------------------------------ deploy (CD of the lambdas)

resource "aws_iam_role" "deploy" {
  name               = "${var.name_prefix}-disaster_recovery-lambdas-deploy-role"
  assume_role_policy = data.aws_iam_policy_document.assume["deploy"].json
}

data "aws_iam_policy_document" "deploy" {
  statement {
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:CompleteLayerUpload",
      "ecr:GetDownloadUrlForLayer",
      "ecr:InitiateLayerUpload",
      "ecr:PutImage",
      "ecr:UploadLayerPart",
    ]
    resources = ["arn:aws:ecr:${var.dr_region}:${local.account_id}:repository/${var.ecr_prefix}/*"]
  }

  statement {
    actions = [
      "lambda:UpdateFunctionCode",
      "lambda:GetFunctionConfiguration",
    ]
    resources = ["arn:aws:lambda:${var.dr_region}:${local.account_id}:function:${var.name_prefix}-*"]
  }
}

resource "aws_iam_role_policy" "deploy" {
  name   = "ci-deploy"
  role   = aws_iam_role.deploy.id
  policy = data.aws_iam_policy_document.deploy.json
}

# ------------------------------------------------------------------ plan (pull requests)

resource "aws_iam_role" "plan" {
  name               = "${var.name_prefix}-disaster_recovery-lambdas-plan-role"
  assume_role_policy = data.aws_iam_policy_document.assume["plan"].json
}

resource "aws_iam_role_policy_attachment" "plan_read_only" {
  role       = aws_iam_role.plan.name
  policy_arn = "arn:aws:iam::aws:policy/ReadOnlyAccess"
}

data "aws_iam_policy_document" "plan_state" {
  statement {
    actions   = ["s3:ListBucket"]
    resources = ["arn:aws:s3:::${var.state_bucket}"]
  }

  statement {
    actions   = ["s3:GetObject"]
    resources = [local.state_object_arn]
  }

  # use_lockfile: plan takes the lock, but must never be able to overwrite the state itself.
  statement {
    actions   = ["s3:PutObject", "s3:DeleteObject"]
    resources = ["${local.state_object_arn}.tflock"]
  }
}

resource "aws_iam_role_policy" "plan_state" {
  name   = "tfstate-read-and-lock"
  role   = aws_iam_role.plan.id
  policy = data.aws_iam_policy_document.plan_state.json
}

# ------------------------------------------------------------------ apply (environment prod)

resource "aws_iam_role" "apply" {
  name               = "${var.name_prefix}-disaster_recovery-lambdas-apply-role"
  assume_role_policy = data.aws_iam_policy_document.assume["apply"].json
}

# Needed by `plan`/refresh of every resource type.
resource "aws_iam_role_policy_attachment" "apply_read_only" {
  role       = aws_iam_role.apply.name
  policy_arn = "arn:aws:iam::aws:policy/ReadOnlyAccess"
}

data "aws_iam_policy_document" "apply" {
  # --- Terraform state of terraform/ only (not the state of this bootstrap stack)
  statement {
    actions   = ["s3:ListBucket"]
    resources = ["arn:aws:s3:::${var.state_bucket}"]
  }

  statement {
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = [local.state_object_arn, "${local.state_object_arn}.tflock"]
  }

  # --- ECR repositories of the lambdas (terraform/ creates nothing else, so no IAM is needed)
  statement {
    actions   = ["ecr:*"]
    resources = ["arn:aws:ecr:${var.dr_region}:${local.account_id}:repository/${var.ecr_prefix}/*"]
  }
}

resource "aws_iam_role_policy" "apply" {
  name   = "terraform-apply"
  role   = aws_iam_role.apply.id
  policy = data.aws_iam_policy_document.apply.json
}