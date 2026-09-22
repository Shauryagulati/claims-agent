# Fargate provisions no capacity, so the cluster is purely a namespace for
# services and tasks. Free while Container Insights is disabled.
resource "aws_ecs_cluster" "main" {
  name = var.project_name
}

# Immutable blueprint. Each apply that changes anything here registers a new
# revision rather than mutating this one, which is what makes rollback a
# matter of pointing the service at an older revision.
resource "aws_ecs_task_definition" "app" {
  family = var.project_name

  requires_compatibilities = ["FARGATE"]

  # Mandatory for Fargate. Gives the task its own ENI, which is why it needs
  # a subnet and a security group of its own.
  network_mode = "awsvpc"

  # Fargate accepts only specific CPU/memory pairs. 256 CPU units (0.25 vCPU)
  # is valid with 512, 1024 or 2048 MB.
  cpu    = 256
  memory = 512

  # Must match the architecture of the pushed image. A mismatch presents as
  # "exec format error" in a crash loop and reads like an application bug.
  runtime_platform {
    cpu_architecture        = "ARM64"
    operating_system_family = "LINUX"
  }

  # Assumed by the ECS agent to pull the image, write logs and read the
  # secret. No task_role_arn: the application calls no AWS APIs.
  execution_role_arn = aws_iam_role.task_execution.arn

  container_definitions = jsonencode([
    {
      name  = var.project_name
      image = "${aws_ecr_repository.app.repository_url}:${var.image_tag}"

      # One container, so its exit stops the task. This is what turns the
      # app's fail-fast startup into a visible ECS event.
      essential = true

      # Under awsvpc the container port is the host port; there is no
      # remapping to configure.
      portMappings = [
        {
          containerPort = 8000
          protocol      = "tcp"
        }
      ]

      # Resolved by the agent at container start from the ARN. The value is
      # not in this definition, not in state, and not in the console.
      secrets = [
        {
          name      = "ANTHROPIC_API_KEY"
          valueFrom = aws_secretsmanager_secret.anthropic_api_key.arn
        }
      ]

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.app.name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "ecs"
        }
      }

      # Deliberately no healthCheck. python:3.11-slim ships no curl or wget,
      # so the reflexive CMD-SHELL curl check would fail every time. The
      # process exiting non-zero is the health signal.
    }
  ])
}
