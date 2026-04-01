output "db_endpoint"       { value = aws_db_instance.main.address }
output "db_name"           { value = aws_db_instance.main.db_name }
output "db_secret_arn"     { value = aws_secretsmanager_secret.db_auth.arn }
output "jwt_secret_arn"    { value = aws_secretsmanager_secret.jwt_secret.arn }
output "secret_arns"       { value = [aws_secretsmanager_secret.db_auth.arn, aws_secretsmanager_secret.jwt_secret.arn] }
