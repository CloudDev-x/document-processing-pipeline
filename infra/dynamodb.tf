resource "aws_dynamodb_table" "documents" {
  name         = "docpipe-documents"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "tenant_id"
  range_key    = "document_id"

  attribute {
    name = "tenant_id"
    type = "S"
  }

  attribute {
    name = "document_id"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  stream_enabled   = true
  stream_view_type = "NEW_AND_OLD_IMAGES"
}
