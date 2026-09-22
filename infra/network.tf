# A dedicated VPC so that "terraform destroy" removes the whole network, and
# so nothing here can affect the account's default VPC.

data "aws_availability_zones" "available" {
  state = "available"
}

resource "aws_vpc" "main" {
  cidr_block = "10.0.0.0/16"

  # Both default to true, but the task depends on them: it resolves the
  # public DNS names of ECR, Secrets Manager, CloudWatch and Anthropic.
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = var.project_name }
}

# One AZ is sufficient for a single task with no load balancer. Production
# would spread across two or three.
resource "aws_subnet" "public" {
  vpc_id            = aws_vpc.main.id
  cidr_block        = "10.0.1.0/24"
  availability_zone = data.aws_availability_zones.available.names[0]

  tags = { Name = "${var.project_name}-public" }
}

# Free. The metered resource we are avoiding is the NAT Gateway, which is
# a different thing entirely.
resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id

  tags = { Name = var.project_name }
}

# This route is what makes the subnet public. Without it the task has a
# public IP and still cannot reach ECR, which looks like a pull timeout.
resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = { Name = "${var.project_name}-public" }
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}
