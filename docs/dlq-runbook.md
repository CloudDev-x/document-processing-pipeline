# Dead-letter queue and workflow runbook

## What reaches what

- **Work queue to DLQ:** only messages the starter Lambda fails to process three times. That means a starter bug or an AWS dependency (DynamoDB, Step Functions) being down. Bad documents never reach the DLQ.
- **Bad documents:** the workflow marks them `FAILED` with `last_error` and finishes successfully (`DocumentRejected`). Examples: non-UTF-8 text, unsupported content type, invalid model output, a Textract job that failed on the file.
- **Unexpected workflow failures:** the execution ends in `Failed` and the `docpipe-workflow-failed` alarm fires.
- **Stuck jobs:** a Textract job that never finishes times out after 20 minutes and fires `docpipe-workflow-timed-out`.

## DLQ alarm (`docpipe-dlq-depth`)

1. Check depth and peek at a message:
   ```bash
   DLQ_URL=$(aws sqs get-queue-url --queue-name docpipe-work-dlq --query QueueUrl --output text)
   aws sqs get-queue-attributes --queue-url "$DLQ_URL" --attribute-names ApproximateNumberOfMessages
   aws sqs receive-message --queue-url "$DLQ_URL" --max-number-of-messages 1 --visibility-timeout 0
   ```
2. Read the starter's errors for that document:
   ```bash
   aws logs tail /aws/lambda/docpipe-worker --since 1h --filter-pattern "<DOCUMENT_ID>"
   ```
3. Fix the cause, then redrive the messages back to the work queue:
   ```bash
   aws sqs start-message-move-task \
     --source-arn arn:aws:sqs:us-east-1:746491202337:docpipe-work-dlq \
     --destination-arn arn:aws:sqs:us-east-1:746491202337:docpipe-work
   ```
4. Purge only after investigation:
   ```bash
   aws sqs purge-queue --queue-url "$DLQ_URL"
   ```

## Workflow failed alarm (`docpipe-workflow-failed`)

1. Find the failed executions:
   ```bash
   SM_ARN=$(aws stepfunctions list-state-machines --query "stateMachines[?name=='docpipe-document'].stateMachineArn" --output text)
   aws stepfunctions list-executions --state-machine-arn "$SM_ARN" --status-filter FAILED --max-results 5
   ```
2. Read the history for the failing step:
   ```bash
   aws stepfunctions get-execution-history --execution-arn "<EXECUTION_ARN>" --reverse-order --max-results 5
   ```
3. Check the document record for `last_error` and `status`.

## Common causes

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| Starter errors with `AccessDeniedException` | Worker role missing a permission | Check `infra/lambda.tf` (`docpipe-worker` policy) |
| Workflow `Failed`, error from `ReadTextract` or `StartTextract` with `AccessDenied` | Steps role missing a Textract or `iam:PassRole` permission | Check `infra/workflow.tf` (`docpipe-steps` policy) |
| Workflow `Failed`, error from `ExtractTextract` or `Extract` with `AccessDenied` | Bedrock access or model ID changed | Check model access and `MODEL_ID` in `infra/lambda.tf` |
| Document stuck at `PROCESSING` | Textract completion never reached the callback | Check the `docpipe-textract-callback` logs and the SNS topic |
| `docpipe-workflow-timed-out` alarm | Textract job never finished | Check the job in the Textract console using `textract_job_id` on the document |
| Document `FAILED` with `Only UTF-8 text documents...` | Binary file uploaded as `text/plain` | Expected. Tell the uploader. |

## Notes on retries

- Transient Bedrock and Textract errors retry inside the workflow with backoff (3 attempts).
- Bad input is never retried. It fails on the first attempt.
- The `attempts` counter on the document counts starter invocations, not workflow retries.
