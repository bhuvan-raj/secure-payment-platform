output "auth_service_role_arn"        { value = aws_iam_role.auth_service.arn }
output "payment_service_role_arn"     { value = aws_iam_role.payment_service.arn }
output "transaction_service_role_arn" { value = aws_iam_role.transaction_service.arn }
