# The EXECUTION role: assumed by the ECS agent, not by the application. It
# pulls the image, creates log streams, and resolves the secret before the
# container starts.
#
# There is deliberately no task role. A task role supplies AWS credentials to
# the application itself, and this application calls no AWS APIs.

data "aws_iam_policy_document" "task_execution_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "task_execution" {
  name               = "${var.project_name}-task-execution"
  assume_role_policy = data.aws_iam_policy_document.task_execution_assume.json
}

# ECR pull plus CloudWatch Logs write. AWS-managed so it tracks new
# requirements as ECS evolves.
resource "aws_iam_role_policy_attachment" "task_execution" {
  role       = aws_iam_role.task_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# Scoped to exactly one secret ARN. The ARN carries a random six-character
# suffix, so it is referenced rather than reconstructed.
#
# No kms:Decrypt statement is needed: the secret uses the AWS-managed
# aws/secretsmanager key, which Secrets Manager decrypts on the caller's
# behalf. A customer-managed key would require one.
data "aws_iam_policy_document" "read_anthropic_secret" {
  statement {
    effect    = "Allow"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.anthropic_api_key.arn]
  }
}

resource "aws_iam_role_policy" "read_anthropic_secret" {
  name   = "read-anthropic-secret"
  role   = aws_iam_role.task_execution.id
  policy = data.aws_iam_policy_document.read_anthropic_secret.json
}
