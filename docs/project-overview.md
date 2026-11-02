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

## Multi-tenancy (Phase 4)

- Each tenant has its own API key, mapped to a tenant ID by the API. The tenant list lives in `infra/api.tf` (`locals.tenants`).
- Documents are keyed by `(tenant_id, document_id)` in DynamoDB. Uploads are stored under `tenant/<uuid>` in S3.
- Listing queries by tenant, so one tenant can never list another's documents.

## Audit trail (Phase 4)

- The uploads bucket has versioning enabled, so overwritten and deleted uploads can be recovered.
- A DynamoDB stream writes one row per status change to `docpipe-audit`, keyed by `tenant#document` and timestamp.

## Notifications (Phase 4)

- Every terminal status (`COMPLETED`, `REVIEW`, `FAILED`) is published to the `docpipe-document-events` SNS topic. Messages carry `tenant_id` and `status` as attributes, so subscribers can filter.
- Delivery is at least once. Subscribers should dedupe on `document_id` and `status`.

## Webhooks (Phase 4)

- Each terminal status is also delivered as a signed HTTP POST to the tenant's webhook URL, if one is configured.
- Delivery path: SNS topic, then an SQS queue, then the `docpipe-webhook` Lambda. Failed deliveries retry through SQS, and after five attempts they go to `docpipe-webhooks-dlq`.
- Each tenant's URL and signing secret live in SSM under `/docpipe/tenants/<tenant>/`. Terraform creates them as `disabled` and ignores later changes, so set real values with `aws ssm put-parameter --overwrite`.
- Requests carry `X-DocPipe-Event-Id`, `X-DocPipe-Timestamp`, and `X-DocPipe-Signature`. The signature is `sha256=` followed by HMAC-SHA256 of `"<timestamp>.<body>"` using the tenant's secret. Receivers should check the signature and reject old timestamps.
- Only HTTPS URLs are used.

## Human review (Phase 4)

- `POST /documents/{id}/review` with `{"decision": "approve" | "reject", "corrections": {...}}`.
- Only documents in `REVIEW` can be reviewed. The update is conditional, so a second reviewer gets a 409.
- Approve sets `COMPLETED`, and reject sets `FAILED`. Corrections are stored on the document. The original extraction is left unchanged, so consumers merge the two.

## Settings in SSM (Phase 4)

- The model ID, review threshold, and extraction prompt live in `/docpipe/model_id`, `/docpipe/review_threshold`, and `/docpipe/extraction_prompt`.
- The steps Lambda reads them once per container, so changes apply to containers that start after the update.
