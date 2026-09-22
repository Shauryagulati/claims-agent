# Private registry for the application image. Fargate pulls from here at
# task start, authenticating with the execution role rather than a stored
# credential.

resource "aws_ecr_repository" "app" {
  name = var.project_name

  # Tags are git SHAs. IMMUTABLE means a tag can never be repointed, so the
  # tag in the task definition identifies exactly one image forever. The
  # trade: the same tag cannot be pushed twice.
  image_tag_mutability = "IMMUTABLE"

  # Lets "terraform destroy" remove the repository along with its images.
  # Appropriate for a demo; the opposite of what a production registry wants.
  force_delete = true

  image_scanning_configuration {
    scan_on_push = true
  }
}

# Without this, every deploy leaves an image behind forever.
resource "aws_ecr_lifecycle_policy" "app" {
  repository = aws_ecr_repository.app.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Expire untagged layers after one day"
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = 1
        }
        action = { type = "expire" }
      },
      {
        rulePriority = 2
        description  = "Keep only the five most recent images"
        selection = {
          tagStatus   = "any"
          countType   = "imageCountMoreThan"
          countNumber = 5
        }
        action = { type = "expire" }
      },
    ]
  })
}
