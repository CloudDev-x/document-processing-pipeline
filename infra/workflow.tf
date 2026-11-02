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
    actions   = ["s3:GetObject", "s3:PutObject"]
    resources = ["${aws_s3_bucket.textract_scratch.arn}/*"]
  }
  statement {
    actions   = ["dynamodb:GetItem", "dynamodb:UpdateItem"]
    resources = [aws_dynamodb_table.documents.arn]
  }
  # Textract actions do not support resource-level permissions.
  statement {
    actions   = ["textract:StartDocumentTextDetection", "textract:GetDocumentTextDetection"]
    resources = ["*"]
  }
  statement {
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.textract_sns.arn]
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
      TABLE_NAME          = aws_dynamodb_table.documents.name
      BUCKET_NAME         = aws_s3_bucket.uploads.id
      SCRATCH_BUCKET_NAME = aws_s3_bucket.textract_scratch.id
      MODEL_ID            = local.model_id
      TEXTRACT_TOPIC_ARN  = aws_sns_topic.textract_done.arn
      TEXTRACT_ROLE_ARN   = aws_iam_role.textract_sns.arn
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
    Comment        = "Document processing: classify, extract text, model call, validation, persistence"
    StartAt        = "Classify"
    TimeoutSeconds = 1800
    States = {
      Classify = {
        Type     = "Task"
        Resource = "arn:aws:states:::lambda:invoke"
        Parameters = {
          FunctionName = aws_lambda_function.steps.arn
          Payload = {
            action          = "classify"
            "document_id.$" = "$.document_id"
          }
        }
        ResultSelector = { "result.$" = "$.Payload" }
        ResultPath     = "$.classification"
        TimeoutSeconds = 30
        Catch          = [{ ErrorEquals = ["States.ALL"], ResultPath = "$.error", Next = "MarkFailed" }]
        Next           = "Route"
      }
      Route = {
        Type = "Choice"
        Choices = [{
          Variable     = "$.classification.result.route"
          StringEquals = "textract"
          Next         = "StartTextract"
        }]
        Default = "Extract"
      }
      StartTextract = {
        Type     = "Task"
        Resource = "arn:aws:states:::lambda:invoke.waitForTaskToken"
        Parameters = {
          FunctionName = aws_lambda_function.steps.arn
          Payload = {
            action          = "start_textract"
            "document_id.$" = "$.document_id"
            "task_token.$"  = "$$.Task.Token"
          }
        }
        ResultPath     = "$.textract"
        TimeoutSeconds = 1200
        Retry = [{
          ErrorEquals     = ["TransientError"]
          IntervalSeconds = 5
          MaxAttempts     = 3
          BackoffRate     = 2
        }]
        Catch = [{ ErrorEquals = ["States.ALL"], ResultPath = "$.error", Next = "MarkFailed" }]
        Next  = "ReadTextract"
      }
      ReadTextract = {
        Type     = "Task"
        Resource = "arn:aws:states:::lambda:invoke"
        Parameters = {
          FunctionName = aws_lambda_function.steps.arn
          Payload = {
            action          = "read_textract"
            "document_id.$" = "$.document_id"
          }
        }
        ResultSelector = { "result.$" = "$.Payload" }
        ResultPath     = "$.textract_text"
        TimeoutSeconds = 60
        Retry = [{
          ErrorEquals     = ["TransientError"]
          IntervalSeconds = 5
          MaxAttempts     = 3
          BackoffRate     = 2
        }]
        Catch = [{ ErrorEquals = ["States.ALL"], ResultPath = "$.error", Next = "MarkFailed" }]
        Next  = "ExtractTextract"
      }
      ExtractTextract = {
        Type     = "Task"
        Resource = "arn:aws:states:::lambda:invoke"
        Parameters = {
          FunctionName = aws_lambda_function.steps.arn
          Payload = {
            action          = "extract"
            "document_id.$" = "$.document_id"
            "text_key.$"    = "$.textract_text.result.text_key"
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
        Cause = "Document processing failed. See last_error in the documents table."
      }
    }
  })
}
