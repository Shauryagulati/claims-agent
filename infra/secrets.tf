# The container only. Terraform never manages the secret's value: there is
# deliberately no aws_secretsmanager_secret_version resource here, because
# state is plaintext JSON and a value Terraform manages is a value Terraform
# stores. The key is written once with "aws secretsmanager put-secret-value".

resource "aws_secretsmanager_secret" "anthropic_api_key" {
  name        = "${var.project_name}/anthropic-api-key"
  description = "Anthropic API key read by the ECS task at container start."

  # Default is 30, which turns "terraform destroy" into a 30-day deletion
  # schedule and blocks recreating the same name until it is purged. Zero
  # deletes immediately, which is what a teardownable demo wants.
  recovery_window_in_days = 0
}
