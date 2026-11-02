output "api_invoke_url" {
  value = aws_api_gateway_stage.v1.invoke_url
}

output "api_key_id" {
  value = aws_api_gateway_api_key.client.id
}
