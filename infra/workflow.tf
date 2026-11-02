data "archive_file" "steps" {
  type        = "zip"
  source_file = "${path.module}/../src/steps.py"
  output_path = "${path.module}/build/steps.zip"
}

resource "aws_iam_role" "steps" {
  name               = "docpipe-steps"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy_attachment" "steps_logs" {
  role       = aws_iam_role.steps.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

data "aws_iam_policy_document" "steps" {
  statement {
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.uploads.arn}/*"]
  }
  statement {
    actions   = ["dynamodb:UpdateItem"]
    resources = [aws_dynamodb_table.documents.arn]
  }
  statement {
    actions = ["bedrock:InvokeModel"]
    resources = [
      "arn:aws:bedrock:us-east-1:${data.aws_caller_identity.current.account_id}:inference-profile/${local.model_id}",
      "arn:aws:bedrock:us-*::foundation-model/anthropic.claude-haiku-4-5-20251001-v1:0",
    ]
  }
}

resource "aws_iam_role_policy" "steps" {
  name   = "docpipe-steps"
  role   = aws_iam_role.steps.id
  policy = data.aws_iam_policy_document.steps.json
}

resource "aws_lambda_function" "steps" {
  function_name    = "docpipe-steps"
  role             = aws_iam_role.steps.arn
  runtime          = "python3.12"
  handler          = "steps.lambda_handler"
  filename         = data.archive_file.steps.output_path
  source_code_hash = data.archive_file.steps.output_base64sha256
  timeout          = 60
  memory_size      = 256

  environment {
    variables = {
      TABLE_NAME  = aws_dynamodb_table.documents.name
      BUCKET_NAME = aws_s3_bucket.uploads.id
      MODEL_ID    = local.model_id
    }
  }
}

resource "aws_iam_role" "workflow" {
  name = "docpipe-workflow"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "states.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "workflow" {
  name = "docpipe-workflow"
  role = aws_iam_role.workflow.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "lambda:InvokeFunction"
      Resource = aws_lambda_function.steps.arn
    }]
  })
}

resource "aws_sfn_state_machine" "document" {
  name     = "docpipe-document"
  role_arn = aws_iam_role.workflow.arn

  definition = jsonencode({
    Comment        = "Document extraction: model call, validation, persistence"
    StartAt        = "Extract"
    TimeoutSeconds = 900
    States = {
      Extract = {
        Type     = "Task"
        Resource = "arn:aws:states:::lambda:invoke"
        Parameters = {
          FunctionName = aws_lambda_function.steps.arn
          Payload = {
            action          = "extract"
            "document_id.$" = "$.document_id"
          }
        }
        ResultSelector = { "result.$" = "$.Payload" }
        ResultPath     = "$.extraction"
        TimeoutSeconds = 90
        Retry = [{
          ErrorEquals     = ["TransientError"]
          IntervalSeconds = 5
          MaxAttempts     = 3
          BackoffRate     = 2
        }]
        Catch = [{ ErrorEquals = ["States.ALL"], ResultPath = "$.error", Next = "MarkFailed" }]
        Next  = "Validate"
      }
      Validate = {
        Type     = "Task"
        Resource = "arn:aws:states:::lambda:invoke"
        Parameters = {
          FunctionName = aws_lambda_function.steps.arn
          Payload = {
            action        = "validate"
            "raw_reply.$" = "$.extraction.result.raw_reply"
          }
        }
        ResultSelector = { "result.$" = "$.Payload" }
        ResultPath     = "$.validation"
        TimeoutSeconds = 30
        Catch          = [{ ErrorEquals = ["States.ALL"], ResultPath = "$.error", Next = "MarkFailed" }]
        Next           = "Persist"
      }
      Persist = {
        Type     = "Task"
        Resource = "arn:aws:states:::lambda:invoke"
        Parameters = {
          FunctionName = aws_lambda_function.steps.arn
          Payload = {
            action          = "persist"
            "document_id.$" = "$.document_id"
            "status.$"      = "$.validation.result.status"
            "result.$"      = "$.validation.result.result"
            "usage.$"       = "$.extraction.result.usage"
          }
        }
        ResultSelector = { "result.$" = "$.Payload" }
        ResultPath     = "$.persisted"
        TimeoutSeconds = 30
        Catch          = [{ ErrorEquals = ["States.ALL"], ResultPath = "$.error", Next = "MarkFailed" }]
        Next           = "Succeed"
      }
      MarkFailed = {
        Type     = "Task"
        Resource = "arn:aws:states:::lambda:invoke"
        Parameters = {
          FunctionName = aws_lambda_function.steps.arn
          Payload = {
            action          = "fail"
            "document_id.$" = "$.document_id"
            "error.$"       = "States.JsonToString($.error)"
          }
        }
        ResultSelector = { "result.$" = "$.Payload" }
        ResultPath     = "$.failure"
        TimeoutSeconds = 30
        Next           = "Failed"
      }
      Succeed = { Type = "Succeed" }
      Failed = {
        Type  = "Fail"
        Error = "DocumentFailed"
        Cause = "Extraction failed. See last_error in the documents table."
      }
    }
  })
}
