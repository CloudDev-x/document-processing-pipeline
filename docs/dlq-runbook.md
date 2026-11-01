# Dead-Letter Queue Runbook

## When to use

The `docpipe-dlq-depth` CloudWatch alarm fires when ≥1 message is in `docpipe-work-dlq`.

## Triage steps

1. **Check DLQ depth**
   ```bash
   DLQ_URL=$(aws sqs get-queue-url --queue-name docpipe-work-dlq --query QueueUrl --output text)
   aws sqs get-queue-attributes --queue-url "$DLQ_URL" --attribute-names ApproximateNumberOfMessages
   ```

2. **Peek at messages** (does not consume them)
   ```bash
   aws sqs receive-message --queue-url "$DLQ_URL" --max-number-of-messages 5 --visibility-timeout 0
   ```

3. **Check the document status in DynamoDB**
   ```bash
   aws dynamodb get-item --table-name docpipe-documents --key '{"document_id":{"S":"<DOCUMENT_ID>"}}'
   ```

4. **Check Lambda logs for the failure**
   ```bash
   aws logs tail /aws/lambda/docpipe-worker --since 1h --filter-pattern "<DOCUMENT_ID>"
   ```

## Common failure modes

| Symptom | Cause | Fix |
|---------|-------|-----|
| `last_error: "Only UTF-8 text documents are supported"` | Binary or non-UTF-8 file uploaded | Delete file from S3; purge DLQ message |
| `last_error` mentions `AccessDeniedException` | IAM permissions or Bedrock access | Check Lambda role policy and model access |
| `last_error` mentions `ReadTimeoutError` | Bedrock latency spike | Retry by redriving the message; if persistent, increase `read_timeout` |
| `status: PROCESSING`, `attempts >= 3` | Claim guard was bypassed (duplicate delivery) | Fixed in claim() — should not recur; manually set status to FAILED |

## Redrive

To reprocess DLQ messages (sends them back to the work queue):

```bash
aws sqs start-message-move-task \
  --source-arn arn:aws:sqs:us-east-1:746491202337:docpipe-work-dlq \
  --destination-arn arn:aws:sqs:us-east-1:746491202337:docpipe-work
```

## Purge

To discard all DLQ messages after investigation:

```bash
aws sqs purge-queue --queue-url "$DLQ_URL"
```
