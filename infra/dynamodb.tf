# One item per document. Holds the status, attempt count, and last error for each upload.
resource "aws_dynamodb_table" "documents" {
  name         = "docpipe-documents"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "document_id"

  attribute {
    name = "document_id"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }
}
