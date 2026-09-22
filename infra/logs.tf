# The container's only output channel. Fargate has no host to inspect, so
# without this the fail-fast startup message is simply lost.

resource "aws_cloudwatch_log_group" "app" {
  name = "/ecs/${var.project_name}"

  # The default is "never expire", which accumulates storage cost forever.
  retention_in_days = 7
}
