# CD role for GitHub Actions OIDC — deploys assume this role, no static keys.
# Modeled on the Plinths infra/iam module; account ID is resolved at apply
# time (never hardcoded) and every resource scope derives from project_prefix.
#
# Usage:
#   terraform init
#   terraform apply           # then: gh variable set AWS_DEPLOY_ROLE_ARN --body "<output>"
terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

data "aws_caller_identity" "current" {}

variable "aws_region" {
  default = "us-east-1"
}

variable "github_org" {
  description = "GitHub username or organization"
  default     = "wakeensito"
}

variable "github_repo" {
  description = "GitHub repository name"
  default     = "aws-daily-discord"
}

variable "github_branch" {
  description = "Branch allowed to assume the role"
  default     = "main"
}

variable "project_prefix" {
  description = "Prefix that scopes every resource this role may touch (stacks, buckets, roles)"
  default     = "aws-daily-discord"
}

variable "create_oidc_provider" {
  description = "Set to true only if the GitHub OIDC provider does not already exist in this AWS account (it can exist only once per account)"
  type        = bool
  default     = false
}

variable "existing_oidc_provider_arn" {
  description = "ARN of the existing GitHub OIDC provider. Required when create_oidc_provider is false. Find it: aws iam list-open-id-connect-providers"
  type        = string
  default     = ""
}

# --- OIDC Provider (created only when create_oidc_provider is true) ---
resource "aws_iam_openid_connect_provider" "github" {
  count           = var.create_oidc_provider ? 1 : 0
  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1"]
}

locals {
  oidc_provider_arn = var.create_oidc_provider ? aws_iam_openid_connect_provider.github[0].arn : var.existing_oidc_provider_arn
}

# Prevent an empty Federated principal (MalformedPolicyDocument) when
# create_oidc_provider is false but existing_oidc_provider_arn was not set.
check "oidc_provider_arn_configured" {
  assert {
    condition     = local.oidc_provider_arn != ""
    error_message = "Set create_oidc_provider = true OR provide existing_oidc_provider_arn (GitHub OIDC provider ARN)."
  }
}

# --- CD Role: only this repo's main branch can assume it ---
resource "aws_iam_role" "cd_role" {
  name = "${var.github_repo}-cd-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Federated = local.oidc_provider_arn
        }
        Action = "sts:AssumeRoleWithWebIdentity"
        Condition = {
          StringEquals = {
            "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
          }
          StringLike = {
            "token.actions.githubusercontent.com:sub" = "repo:${var.github_org}/${var.github_repo}:ref:refs/heads/${var.github_branch}"
          }
        }
      }
    ]
  })
}

resource "aws_iam_role_policy" "cd_policy" {
  name = "${var.github_repo}-cd-policy"
  role = aws_iam_role.cd_role.id
  policy = templatefile("${path.module}/cd-role-policy.json.tpl", {
    account_id = data.aws_caller_identity.current.account_id
    region     = var.aws_region
    prefix     = var.project_prefix
  })
}

output "cd_role_arn" {
  description = "ARN to set as AWS_DEPLOY_ROLE_ARN in GitHub repo variables"
  value       = aws_iam_role.cd_role.arn
}
