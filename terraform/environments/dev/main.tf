terraform {
  required_version = ">= 1.7.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.50"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.30"
    }
  }

  backend "s3" {
    bucket         = "secure-payment-tfstate-dev"
    key            = "dev/terraform.tfstate"
    region         = "us-east-1"
    encrypt        = true
    dynamodb_table = "terraform-state-lock"
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Environment = var.environment
      Project     = "secure-payment-platform"
      ManagedBy   = "terraform"
      Team        = "platform-engineering"
    }
  }
}

# ── VPC ───────────────────────────────────────────────────────────────────────
module "vpc" {
  source = "../../modules/vpc"

  environment          = var.environment
  vpc_cidr             = var.vpc_cidr
  availability_zones   = var.availability_zones
  private_subnet_cidrs = var.private_subnet_cidrs
  public_subnet_cidrs  = var.public_subnet_cidrs
  cluster_name         = local.cluster_name
}

# ── KMS ───────────────────────────────────────────────────────────────────────
module "kms" {
  source      = "../../modules/kms"
  environment = var.environment
  cluster_name = local.cluster_name
}

# ── EKS ───────────────────────────────────────────────────────────────────────
module "eks" {
  source = "../../modules/eks"

  environment        = var.environment
  cluster_name       = local.cluster_name
  cluster_version    = var.eks_cluster_version
  vpc_id             = module.vpc.vpc_id
  private_subnet_ids = module.vpc.private_subnet_ids
  kms_key_arn        = module.kms.eks_kms_key_arn
  node_instance_type = var.node_instance_type
  node_desired_size  = var.node_desired_size
  node_min_size      = var.node_min_size
  node_max_size      = var.node_max_size
}

# ── RDS ───────────────────────────────────────────────────────────────────────
module "rds" {
  source = "../../modules/rds"

  environment        = var.environment
  vpc_id             = module.vpc.vpc_id
  private_subnet_ids = module.vpc.private_subnet_ids
  kms_key_arn        = module.kms.rds_kms_key_arn
  instance_class     = var.rds_instance_class
  eks_security_group_id = module.eks.node_security_group_id
}

# ── IAM / IRSA ────────────────────────────────────────────────────────────────
module "iam" {
  source = "../../modules/iam"

  environment        = var.environment
  cluster_name       = local.cluster_name
  oidc_provider_arn  = module.eks.oidc_provider_arn
  oidc_provider_url  = module.eks.oidc_provider_url
  aws_region         = var.aws_region
  aws_account_id     = data.aws_caller_identity.current.account_id
  payment_queue_arn  = module.security.payment_queue_arn
  secrets_arns       = module.rds.secret_arns
}

# ── Security (WAF, GuardDuty, CloudTrail, SQS) ───────────────────────────────
module "security" {
  source = "../../modules/security"

  environment     = var.environment
  aws_region      = var.aws_region
  aws_account_id  = data.aws_caller_identity.current.account_id
  kms_key_arn     = module.kms.general_kms_key_arn
}

# ── Data sources ──────────────────────────────────────────────────────────────
data "aws_caller_identity" "current" {}

locals {
  cluster_name = "secure-payment-${var.environment}"
}
