resource "aws_sqs_queue" "webhook_dlq" {
  name                      = "docpipe-webhooks-dlq"
  message_retention_seconds = 1209600
  sqs_managed_sse_enabled   = true
}

resource "aws_sqs_queue" "webhooks" {
  name                       = "docpipe-webhooks"
  visibility_timeout_seconds = 60
  message_retention_seconds  = 345600
  sqs_managed_sse_enabled    = true

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.webhook_dlq.arn
    maxReceiveCount     = 5
  })
}

resource "aws_sqs_queue_policy" "webhooks" {
  queue_url = aws_sqs_queue.webhooks.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "sns.amazonaws.com" }
      Action    = "sqs:SendMessage"
      Resource  = aws_sqs_queue.webhooks.arn
      Condition = {
        ArnEquals = { "aws:SourceArn" = aws_sns_topic.document_events.arn }
      }
    }]
  })
}

resource "aws_sns_topic_subscription" "webhooks" {
  topic_arn = aws_sns_topic.document_events.arn
  protocol  = "sqs"
  endpoint  = aws_sqs_queue.webhooks.arn
}

data "archive_file" "webhook" {
  type        = "zip"
  source_file = "${path.module}/../src/webhook_delivery.py"
  output_path = "${path.module}/build/webhook.zip"
}

resource "aws_iam_role" "webhook" {
  name               = "docpipe-webhook"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy_attachment" "webhook_logs" {
  role       = aws_iam_role.webhook.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy_attachment" "webhook_queue" {
  role       = aws_iam_role.webhook.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaSQSQueueExecutionRole"
}

resource "aws_iam_role_policy" "webhook_config" {
  name = "docpipe-webhook-config"
  role = aws_iam_role.webhook.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["ssm:GetParameters"]
      Resource = "arn:aws:ssm:us-east-1:${data.aws_caller_identity.current.account_id}:parameter/docpipe/tenants/*"
    }]
  })
}

resource "aws_lambda_function" "webhook" {
  function_name    = "docpipe-webhook"
  role             = aws_iam_role.webhook.arn
  runtime          = "python3.12"
  handler          = "webhook_delivery.lambda_handler"
  filename         = data.archive_file.webhook.output_path
  source_code_hash = data.archive_file.webhook.output_base64sha256
  timeout          = 30
  memory_size      = 256
}

resource "aws_lambda_event_source_mapping" "webhook" {
  event_source_arn = aws_sqs_queue.webhooks.arn
  function_name    = aws_lambda_function.webhook.arn
  batch_size       = 1
}

# Each tenant's endpoint and signing secret. Terraform creates placeholders. Set the real values with
# `aws ssm put-parameter ... --overwrite`. Terraform ignores later changes, so it won't reset them.
resource "aws_ssm_parameter" "webhook_url" {
  for_each = local.tenants
  name     = "/docpipe/tenants/${each.key}/webhook_url"
  type     = "String"
  value    = "disabled"

  lifecycle {
    ignore_changes = [value]
  }
}

resource "aws_ssm_parameter" "webhook_secret" {
  for_each = local.tenants
  name     = "/docpipe/tenants/${each.key}/webhook_secret"
  type     = "SecureString"
  value    = "disabled"

  lifecycle {
    ignore_changes = [value]
  }
}
