data "archive_file" "api" {
  type        = "zip"
  source_file = "${path.module}/../src/api_handler.py"
  output_path = "${path.module}/build/api.zip"
}

# Tenants. Each gets its own API key, so usage plans throttle and cap each tenant separately.
locals {
  tenants = toset(["acme", "globex"])
}

resource "aws_iam_role" "api" {
  name               = "docpipe-api"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy_attachment" "api_logs" {
  role       = aws_iam_role.api.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "api" {
  name = "docpipe-api"
  role = aws_iam_role.api.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:PutObject"]
        Resource = "${aws_s3_bucket.uploads.arn}/*"
      },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:Query"]
        Resource = aws_dynamodb_table.documents.arn
      },
    ]
  })
}

resource "aws_lambda_function" "api" {
  function_name    = "docpipe-api"
  role             = aws_iam_role.api.arn
  runtime          = "python3.12"
  handler          = "api_handler.lambda_handler"
  filename         = data.archive_file.api.output_path
  source_code_hash = data.archive_file.api.output_base64sha256
  timeout          = 10
  memory_size      = 256

  environment {
    variables = {
      BUCKET_NAME      = aws_s3_bucket.uploads.bucket
      TABLE_NAME       = aws_dynamodb_table.documents.name
      TENANT_BY_KEY_ID = jsonencode({ for t, k in aws_api_gateway_api_key.tenant : k.id => t })
    }
  }
}

resource "aws_api_gateway_rest_api" "docpipe" {
  name = "docpipe-api"

  endpoint_configuration {
    types = ["REGIONAL"]
  }
}

resource "aws_api_gateway_resource" "documents" {
  rest_api_id = aws_api_gateway_rest_api.docpipe.id
  parent_id   = aws_api_gateway_rest_api.docpipe.root_resource_id
  path_part   = "documents"
}

resource "aws_api_gateway_resource" "document_by_id" {
  rest_api_id = aws_api_gateway_rest_api.docpipe.id
  parent_id   = aws_api_gateway_resource.documents.id
  path_part   = "{id}"
}

resource "aws_api_gateway_method" "post_documents" {
  rest_api_id      = aws_api_gateway_rest_api.docpipe.id
  resource_id      = aws_api_gateway_resource.documents.id
  http_method      = "POST"
  authorization    = "NONE"
  api_key_required = true
}

resource "aws_api_gateway_method" "get_documents" {
  rest_api_id      = aws_api_gateway_rest_api.docpipe.id
  resource_id      = aws_api_gateway_resource.documents.id
  http_method      = "GET"
  authorization    = "NONE"
  api_key_required = true
}

resource "aws_api_gateway_method" "get_document" {
  rest_api_id      = aws_api_gateway_rest_api.docpipe.id
  resource_id      = aws_api_gateway_resource.document_by_id.id
  http_method      = "GET"
  authorization    = "NONE"
  api_key_required = true
}

resource "aws_api_gateway_integration" "post_documents" {
  rest_api_id             = aws_api_gateway_rest_api.docpipe.id
  resource_id             = aws_api_gateway_resource.documents.id
  http_method             = aws_api_gateway_method.post_documents.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.api.invoke_arn
}

resource "aws_api_gateway_integration" "get_documents" {
  rest_api_id             = aws_api_gateway_rest_api.docpipe.id
  resource_id             = aws_api_gateway_resource.documents.id
  http_method             = aws_api_gateway_method.get_documents.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.api.invoke_arn
}

resource "aws_api_gateway_integration" "get_document" {
  rest_api_id             = aws_api_gateway_rest_api.docpipe.id
  resource_id             = aws_api_gateway_resource.document_by_id.id
  http_method             = aws_api_gateway_method.get_document.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.api.invoke_arn
}

resource "aws_lambda_permission" "api_gateway" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.api.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.docpipe.execution_arn}/*/*"
}

resource "aws_api_gateway_deployment" "docpipe" {
  rest_api_id = aws_api_gateway_rest_api.docpipe.id

  triggers = {
    redeploy = sha1(jsonencode([
      aws_api_gateway_method.post_documents.id,
      aws_api_gateway_method.get_documents.id,
      aws_api_gateway_method.get_document.id,
      aws_api_gateway_integration.post_documents.id,
      aws_api_gateway_integration.get_documents.id,
      aws_api_gateway_integration.get_document.id,
    ]))
  }

}

resource "aws_api_gateway_stage" "v1" {
  rest_api_id   = aws_api_gateway_rest_api.docpipe.id
  deployment_id = aws_api_gateway_deployment.docpipe.id
  stage_name    = "v1"
}

resource "aws_api_gateway_usage_plan" "default" {
  name = "docpipe-default"

  api_stages {
    api_id = aws_api_gateway_rest_api.docpipe.id
    stage  = aws_api_gateway_stage.v1.stage_name
  }

  throttle_settings {
    rate_limit  = 10
    burst_limit = 20
  }

  quota_settings {
    limit  = 10000
    period = "MONTH"
  }
}

resource "aws_api_gateway_api_key" "tenant" {
  for_each = local.tenants
  name     = "docpipe-${each.key}"
  enabled  = true
}

resource "aws_api_gateway_usage_plan_key" "tenant" {
  for_each      = local.tenants
  key_id        = aws_api_gateway_api_key.tenant[each.key].id
  key_type      = "API_KEY"
  usage_plan_id = aws_api_gateway_usage_plan.default.id
}
