# Keeps one copy of the task definition running and replaces it if it dies.
# No load_balancer block: the task's own public IP is the endpoint.

resource "aws_ecs_service" "app" {
  name            = var.project_name
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.app.arn
  desired_count   = var.desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets         = [aws_subnet.public.id]
    security_groups = [aws_security_group.task.id]

    # Load-bearing, not cosmetic. With no NAT Gateway this public address is
    # the task's only route to ECR, Secrets Manager, CloudWatch and
    # api.anthropic.com. Without it the task cannot even pull its image.
    assign_public_ip = true
  }

  # A failing deployment reverts instead of crash-looping forever. On a first
  # deployment there is no prior version, so it fails cleanly instead.
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }
}
