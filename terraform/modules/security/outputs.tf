output "payment_queue_url" { value = aws_sqs_queue.payment.id }
output "payment_queue_arn" { value = aws_sqs_queue.payment.arn }
output "payment_dlq_arn"   { value = aws_sqs_queue.payment_dlq.arn }
output "waf_acl_arn"       { value = aws_wafv2_web_acl.main.arn }
output "cloudtrail_bucket" { value = aws_s3_bucket.cloudtrail.bucket }
