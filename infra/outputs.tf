output "api_invoke_url" {
  value = aws_api_gateway_stage.v1.invoke_url
}

output "tenant_api_key_ids" {
  value = { for t, k in aws_api_gateway_api_key.tenant : t => k.id }
}
