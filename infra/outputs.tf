output "ecr_repository_url" {
  description = "Registry path to tag and push the image to."
  value       = aws_ecr_repository.app.repository_url
}

output "anthropic_secret_arn" {
  description = "ARN of the secret container. The value is set outside Terraform."
  value       = aws_secretsmanager_secret.anthropic_api_key.arn
}
