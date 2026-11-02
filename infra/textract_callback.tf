resource "aws_sns_topic" "textract_done" {
  name = "docpipe-textract-done"
}

resource "aws_iam_role" "textract_sns" {
  name = "docpipe-textract-sns"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "textract.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "textract_sns" {
  name = "docpipe-textract-sns-publish"
  role = aws_iam_role.textract_sns.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "sns:Publish"
      Resource = aws_sns_topic.textract_done.arn
    }]
  })
}

resource "aws_iam_role" "textract_callback" {
  name               = "docpipe-textract-callback"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy_attachment" "textract_callback_logs" {
  role       = aws_iam_role.textract_callback.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "textract_callback" {
  name = "docpipe-textract-callback"
  role = aws_iam_role.textract_callback.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:UpdateItem"]
        Resource = aws_dynamodb_table.documents.arn
      },
      {
        Effect   = "Allow"
        Action   = ["states:SendTaskSuccess", "states:SendTaskFailure"]
        Resource = "*"
      },
    ]
  })
}

data "archive_file" "textract_callback" {
  type        = "zip"
  source_file = "${path.module}/../src/textract_callback.py"
  output_path = "${path.module}/build/textract_callback.zip"
}

resource "aws_lambda_function" "textract_callback" {
  function_name    = "docpipe-textract-callback"
  role             = aws_iam_role.textract_callback.arn
  runtime          = "python3.12"
  handler          = "textract_callback.lambda_handler"
  filename         = data.archive_file.textract_callback.output_path
  source_code_hash = data.archive_file.textract_callback.output_base64sha256
  timeout          = 10
  memory_size      = 256

  environment {
    variables = {
      TABLE_NAME = aws_dynamodb_table.documents.name
    }
  }
}

resource "aws_sns_topic_subscription" "textract_callback" {
  topic_arn = aws_sns_topic.textract_done.arn
  protocol  = "lambda"
  endpoint  = aws_lambda_function.textract_callback.arn
}

resource "aws_lambda_permission" "textract_sns_invoke" {
  statement_id  = "AllowSNSInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.textract_callback.function_name
  principal     = "sns.amazonaws.com"
  source_arn    = aws_sns_topic.textract_done.arn
}
