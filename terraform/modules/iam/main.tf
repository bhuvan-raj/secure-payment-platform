locals {
  oidc_host = replace(var.oidc_provider_url, "https://", "")
}

# ── Auth Service IRSA ─────────────────────────────────────────────────────────
resource "aws_iam_role" "auth_service" {
  name = "${var.cluster_name}-auth-service-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRoleWithWebIdentity"
      Principal = { Federated = var.oidc_provider_arn }
      Condition = {
        StringEquals = {
          "${local.oidc_host}:sub" = "system:serviceaccount:payment-platform:auth-service-sa"
          "${local.oidc_host}:aud" = "sts.amazonaws.com"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "auth_service" {
  name = "auth-service-policy"
  role = aws_iam_role.auth_service.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "SecretsManagerAccess"
        Effect = "Allow"
        Action = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
        Resource = var.secrets_arns
      }
    ]
  })
}

# ── Payment Service IRSA ──────────────────────────────────────────────────────
resource "aws_iam_role" "payment_service" {
  name = "${var.cluster_name}-payment-service-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRoleWithWebIdentity"
      Principal = { Federated = var.oidc_provider_arn }
      Condition = {
        StringEquals = {
          "${local.oidc_host}:sub" = "system:serviceaccount:payment-platform:payment-service-sa"
          "${local.oidc_host}:aud" = "sts.amazonaws.com"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "payment_service" {
  name = "payment-service-policy"
  role = aws_iam_role.payment_service.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "SecretsManagerAccess"
        Effect = "Allow"
        Action = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
        Resource = var.secrets_arns
      },
      {
        Sid    = "SQSSendMessage"
        Effect = "Allow"
        Action = ["sqs:SendMessage", "sqs:GetQueueAttributes", "sqs:GetQueueUrl"]
        Resource = [var.payment_queue_arn]
      }
    ]
  })
}

# ── Transaction Service IRSA ──────────────────────────────────────────────────
resource "aws_iam_role" "transaction_service" {
  name = "${var.cluster_name}-transaction-service-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRoleWithWebIdentity"
      Principal = { Federated = var.oidc_provider_arn }
      Condition = {
        StringEquals = {
          "${local.oidc_host}:sub" = "system:serviceaccount:payment-platform:transaction-service-sa"
          "${local.oidc_host}:aud" = "sts.amazonaws.com"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "transaction_service" {
  name = "transaction-service-policy"
  role = aws_iam_role.transaction_service.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "SecretsManagerAccess"
        Effect = "Allow"
        Action = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
        Resource = var.secrets_arns
      },
      {
        Sid    = "SQSConsume"
        Effect = "Allow"
        Action = [
          "sqs:ReceiveMessage", "sqs:DeleteMessage",
          "sqs:GetQueueAttributes", "sqs:ChangeMessageVisibility"
        ]
        Resource = [var.payment_queue_arn]
      }
    ]
  })
}
