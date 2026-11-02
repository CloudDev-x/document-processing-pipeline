# Every object version is kept, so an overwritten or deleted upload can be recovered.
resource "aws_s3_bucket_versioning" "uploads" {
  bucket = aws_s3_bucket.uploads.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_dynamodb_table" "audit" {
  name         = "docpipe-audit"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"

  attribute {
    name = "pk"
    type = "S"
  }

  attribute {
    name = "sk"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }
}

resource "aws_sns_topic" "document_events" {
  name = "docpipe-document-events"
}

data "archive_file" "stream" {
  type        = "zip"
  source_file = "${path.module}/../src/stream_handler.py"
  output_path = "${path.module}/build/stream.zip"
}

resource "aws_iam_role" "stream" {
  name               = "docpipe-stream"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy_attachment" "stream_logs" {
  role       = aws_iam_role.stream.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy_attachment" "stream_dynamodb" {
  role       = aws_iam_role.stream.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaDynamoDBExecutionRole"
}

resource "aws_iam_role_policy" "stream" {
  name = "docpipe-stream"
  role = aws_iam_role.stream.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem"]
        Resource = aws_dynamodb_table.audit.arn
      },
      {
        Effect   = "Allow"
        Action   = ["sns:Publish"]
        Resource = aws_sns_topic.document_events.arn
      },
    ]
  })
}

resource "aws_lambda_function" "stream" {
  function_name    = "docpipe-stream"
  role             = aws_iam_role.stream.arn
  runtime          = "python3.12"
  handler          = "stream_handler.lambda_handler"
  filename         = data.archive_file.stream.output_path
  source_code_hash = data.archive_file.stream.output_base64sha256
  timeout          = 30
  memory_size      = 256

  environment {
    variables = {
      AUDIT_TABLE_NAME = aws_dynamodb_table.audit.name
      EVENTS_TOPIC_ARN = aws_sns_topic.document_events.arn
    }
  }
}

resource "aws_lambda_event_source_mapping" "stream" {
  event_source_arn               = aws_dynamodb_table.documents.stream_arn
  function_name                  = aws_lambda_function.stream.arn
  starting_position              = "LATEST"
  batch_size                     = 10
  maximum_retry_attempts         = 3
  bisect_batch_on_function_error = true
}
