variable "environment"       { type = string }
variable "cluster_name"      { type = string }
variable "oidc_provider_arn" { type = string }
variable "oidc_provider_url" { type = string }
variable "aws_region"        { type = string }
variable "aws_account_id"    { type = string }
variable "payment_queue_arn" { type = string }
variable "secrets_arns"      { type = list(string) }
