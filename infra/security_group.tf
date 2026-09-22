# Attached to the task's ENI. Stateful: replies to connections the task
# opens are allowed automatically, so no reverse rules are written.

resource "aws_security_group" "task" {
  name        = "${var.project_name}-task"
  description = "Inbound 8000 from the operator only; unrestricted egress."
  vpc_id      = aws_vpc.main.id

  tags = { Name = "${var.project_name}-task" }
}

# The application has no authentication and every answered message spends
# Anthropic credit, so the source is a single operator address.
resource "aws_vpc_security_group_ingress_rule" "app_port" {
  security_group_id = aws_security_group.task.id
  description       = "Operator access to the chat API"

  cidr_ipv4   = var.allowed_cidr
  ip_protocol = "tcp"
  from_port   = 8000
  to_port     = 8000
}

# Required outbound: ECR image pull, Secrets Manager, CloudWatch Logs, and
# api.anthropic.com. With no NAT Gateway these all exit via the IGW.
resource "aws_vpc_security_group_egress_rule" "all" {
  security_group_id = aws_security_group.task.id
  description       = "All outbound"

  cidr_ipv4   = "0.0.0.0/0"
  ip_protocol = "-1"
}
