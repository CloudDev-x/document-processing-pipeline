resource "aws_api_gateway_resource" "review" {
  rest_api_id = aws_api_gateway_rest_api.docpipe.id
  parent_id   = aws_api_gateway_resource.document_by_id.id
  path_part   = "review"
}

resource "aws_api_gateway_method" "review" {
  rest_api_id      = aws_api_gateway_rest_api.docpipe.id
  resource_id      = aws_api_gateway_resource.review.id
  http_method      = "POST"
  authorization    = "NONE"
  api_key_required = true
}

resource "aws_api_gateway_integration" "review" {
  rest_api_id             = aws_api_gateway_rest_api.docpipe.id
  resource_id             = aws_api_gateway_resource.review.id
  http_method             = aws_api_gateway_method.review.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.api.invoke_arn
}
