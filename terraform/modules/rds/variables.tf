variable "environment"           { type = string }
variable "vpc_id"                { type = string }
variable "private_subnet_ids"    { type = list(string) }
variable "kms_key_arn"           { type = string }
variable "instance_class"        { type = string }
variable "eks_security_group_id" { type = string }
