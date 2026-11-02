# Document Processing Pipeline

A serverless pipeline that accepts uploaded documents, extracts structured data with Amazon Bedrock (Claude Haiku), and stores the results with status tracking. Everything is defined in Terraform and deployed through GitHub Actions with OIDC.

See [architecture.md](architecture.md) for the diagram.

## Components

| Component | Name | Role |
|-----------|------|------|
| API Gateway | `docpipe-api` | Endpoints for uploads and status, protected by an API key and usage plan |
| Lambda | `docpipe-api` | Creates presigned POST uploads and reads documents |
| S3 | `docpipe-uploads-<account>` | Document storage, encrypted, public access blocked |
| SQS | `docpipe-work`, `docpipe-work-dlq` | Ingest queue (120s visibility, max 3 receives) and dead-letter queue |
| Lambda | `docpipe-worker` | Starter: claims the document and starts one workflow execution |
| Step Functions | `docpipe-document` | Orchestrates classify, extraction, validation, and persistence |
| Lambda | `docpipe-steps` | Workflow steps: classify, Textract start and read, model call, validate, persist, fail |
| Textract | (managed) | Async text detection for PDFs and images |
| SNS and Lambda | `docpipe-textract-done`, `docpipe-textract-callback` | Textract completion resumes the waiting workflow |
| S3 | `docpipe-textract-scratch-<account>` | Extracted text, deleted after one day |
| DynamoDB | `docpipe-documents` | Document status, extraction, and job state |
| CloudWatch | 5 alarms and a dashboard | DLQ depth, queue age, starter errors, workflow failures and timeouts |

## Processing flow

1. The client asks `POST /documents` for a presigned upload and posts the file straight to S3.
2. S3 publishes an event to the work queue.
3. The starter claims the document (status `PROCESSING`, attempts incremented). Duplicate events are ignored.
4. The starter starts a Step Functions execution, named from the document ID and receive count, so duplicate starts are rejected.
5. Classify reads the content type. Text goes straight to extraction. PDFs and images go to Textract.
6. For Textract, the workflow stores its task token on the document and starts an async job tagged with the document ID. Textract publishes to SNS when the job finishes. The callback claims the token once and resumes the workflow.
7. The extracted text is read into the scratch bucket and sent to the model.
8. The model reply is parsed and validated. Confidence of 0.8 or more is `COMPLETED`. Below that is `REVIEW`.
9. The result is persisted with token counts and the model ID.

## Status lifecycle

| Status | Meaning |
|--------|---------|
| `PROCESSING` | Claimed, workflow in progress |
| `COMPLETED` | Extraction at or above the confidence threshold |
| `REVIEW` | Extraction below the threshold, for a human to check |
| `FAILED` | Rejected (bad input or Textract failure), or an unexpected failure. `last_error` says which. |

## Reliability

- **Idempotent claim:** the conditional update rejects documents already `COMPLETED` or `FAILED`.
- **Duplicate starts:** execution names are derived from the document ID and receive count, so Step Functions rejects a repeat start.
- **Single-use Textract resume:** the callback removes the task token with a conditional update before resuming, so a duplicate SNS message can't resume the workflow twice.
- **Retries:** transient Bedrock and Textract errors retry inside the workflow with backoff. Bad input is never retried.
- **Timeouts:** the workflow is capped at 30 minutes, and the Textract wait at 20 minutes.

## Known limitations

- The list endpoint scans the table. A global secondary index on status would replace it.
- The deploy role has `AdministratorAccess`. A scoped policy is the next step.
- API keys, not Cognito, authenticate clients.
- Multi-page PDFs with partial OCR success are rejected rather than sent to review.
- The Lambda code hash differs between machines, so a local `terraform plan` always shows one in-place update on the worker. CI is the source of truth.

## Testing

Unit tests run on every push in CI:
- Starter: claim, execution naming, duplicate handling
- Steps: extraction, validation, persistence, failure, Textract start and read, classification
- Textract helper: pagination, status handling, timeouts
- Callback: resume, failure, duplicate delivery, missing token
- API: routes, validation errors, serialisation

Manual end-to-end results:

| Test | Result |
|------|--------|
| Text statement via API | `COMPLETED`, confidence 0.95 |
| Binary file via S3 | `FAILED` on the first attempt, execution succeeds (`DocumentRejected`) |
| PDF brief via API and Textract | `COMPLETED`, extraction from the document |
| Load test, 20 text documents | 20 of 20 `COMPLETED` |

## Deployment

Pushing to `main` runs CI: unit tests, Terraform format and validate, then `terraform apply` through an OIDC role. The Terraform state lives in S3 with locking.
