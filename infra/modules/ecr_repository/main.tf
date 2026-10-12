locals {
  repository_name = join("/", compact([var.ecr_prefix, var.service, var.name]))
}

resource "aws_ecr_repository" "this" {
  name                 = local.repository_name
  image_tag_mutability = "MUTABLE" # CI also pushes a moving `latest` tag

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "this" {
  repository = aws_ecr_repository.this.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the last ${var.images_to_keep} images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = var.images_to_keep
      }
      action = { type = "expire" }
    }]
  })
}