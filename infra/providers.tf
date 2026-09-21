# default_tags are applied to every taggable resource this configuration
# creates. They are how you filter Cost Explorer to just this project, and
# how you identify a stray resource months from now.

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project   = var.project_name
      ManagedBy = "terraform"
    }
  }
}
