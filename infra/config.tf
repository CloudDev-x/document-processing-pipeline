resource "aws_ssm_parameter" "model_id" {
  name  = "/docpipe/model_id"
  type  = "String"
  value = local.model_id
}

resource "aws_ssm_parameter" "review_threshold" {
  name  = "/docpipe/review_threshold"
  type  = "String"
  value = "0.8"
}

resource "aws_ssm_parameter" "extraction_prompt" {
  name  = "/docpipe/extraction_prompt"
  type  = "String"
  value = <<-EOT
You classify and extract data from a customer document.
Return only a JSON object with these keys:
"document_type": one of "statement", "id", "loan_form", "other"
"confidence": a number from 0 to 1
"fields": an object of key fields you can read from the document, with string values
Do not include any other text.
EOT
}

resource "aws_iam_role_policy" "steps_config" {
  name = "docpipe-steps-config"
  role = aws_iam_role.steps.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["ssm:GetParameters"]
      Resource = "arn:aws:ssm:us-east-1:${data.aws_caller_identity.current.account_id}:parameter/docpipe/*"
    }]
  })
}
