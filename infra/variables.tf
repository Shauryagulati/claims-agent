variable "aws_region" {
  description = "Region for every resource in this configuration."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Name prefix and Project tag for every resource."
  type        = string
  default     = "claims-agent"
}

# Deliberately has no default. The app is unauthenticated and every message
# it answers spends Anthropic credit, so the allowed range is always an
# explicit decision.
variable "allowed_cidr" {
  description = "CIDR permitted to reach the task on port 8000, e.g. 203.0.113.4/32."
  type        = string

  validation {
    condition     = can(cidrnetmask(var.allowed_cidr))
    error_message = "allowed_cidr must be valid CIDR notation, such as 203.0.113.4/32."
  }
}

# Set explicitly rather than derived, so the running revision always names an
# exact image. In a pipeline this is supplied by CI from the build commit.
variable "image_tag" {
  description = "Git SHA tag of the image in ECR to run."
  type        = string
}

# Set to 0 to park the deployment: the task stops and Fargate compute and the
# public IPv4 charge both end, while the rest of the stack stays intact.
variable "desired_count" {
  description = "Number of tasks the service keeps running."
  type        = number
  default     = 1
}
