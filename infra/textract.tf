resource "aws_s3_bucket" "textract_scratch" {
  bucket = "docpipe-textract-scratch-${data.aws_caller_identity.current.account_id}"
}

resource "aws_s3_bucket_public_access_block" "textract_scratch" {
  bucket                  = aws_s3_bucket.textract_scratch.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "textract_scratch" {
  bucket = aws_s3_bucket.textract_scratch.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# Scratch space for extracted text. Nothing here outlives a day.
resource "aws_s3_bucket_lifecycle_configuration" "textract_scratch" {
  bucket = aws_s3_bucket.textract_scratch.id

  rule {
    id     = "expire-scratch"
    status = "Enabled"

    filter {}

    expiration {
      days = 1
    }
  }
}
