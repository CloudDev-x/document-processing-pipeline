# Dead letter queue: holds messages that failed too many times. Kept for the maximum 14 days so they can be inspected.
resource "aws_sqs_queue" "work_dlq" {
  name                      = "docpipe-work-dlq"
  message_retention_seconds = 1209600
  sqs_managed_sse_enabled   = true
}

# Work queue: one message per uploaded document.
resource "aws_sqs_queue" "work" {
  name                       = "docpipe-work"
  visibility_timeout_seconds = 120    # must exceed the processing Lambda's timeout, added later
  message_retention_seconds  = 345600 # 4 days
  sqs_managed_sse_enabled    = true

  # After 3 failed receives, SQS moves the message to the DLQ.
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.work_dlq.arn
    maxReceiveCount     = 3
  })
}

# Allow only this bucket, in this account, to send messages to the work queue.
resource "aws_sqs_queue_policy" "work" {
  queue_url = aws_sqs_queue.work.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "AllowS3UploadEvents"
      Effect    = "Allow"
      Principal = { Service = "s3.amazonaws.com" }
      Action    = "sqs:SendMessage"
      Resource  = aws_sqs_queue.work.arn
      Condition = {
        ArnEquals    = { "aws:SourceArn" = aws_s3_bucket.uploads.arn }
        StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id }
      }
    }]
  })
}

# Send an event to the work queue whenever an object is created in the uploads bucket.
resource "aws_s3_bucket_notification" "uploads" {
  bucket = aws_s3_bucket.uploads.id

  queue {
    queue_arn = aws_sqs_queue.work.arn
    events    = ["s3:ObjectCreated:*"]
  }

  depends_on = [aws_sqs_queue_policy.work]
}
