# Document Processing Pipeline

A serverless pipeline on AWS. Tenants upload documents through a presigned URL. The pipeline extracts structured data with Amazon Bedrock (Claude Haiku), routes uncertain results to human review, records every status change, and notifies tenants by SNS and signed webhooks. Infrastructure is defined in Terraform and deployed from GitHub Actions with OIDC.

- Architecture diagram: [docs/architecture.md](docs/architecture.md)
- Detailed design: [docs/project-overview.md](docs/project-overview.md)
- Operations: [docs/dlq-runbook.md](docs/dlq-runbook.md)

## The problem

Businesses receive large volumes of documents such as bank statements, IDs, and loan forms. Someone has to read each one, type the key details into a system, and check them. That work is slow, expensive, and error-prone. It also raises security and compliance questions: documents contain personal data, customers must not see each other's files, and an auditor needs to know who changed what and when.

Automating it with AI creates new problems. A model can be confidently wrong, a burst of uploads can overwhelm the services it depends on, and a duplicate message can charge twice or notify a customer twice.

## How this pipeline solves it

| Problem | What the pipeline does |
|---------|------------------------|
| Manual data entry is slow and costly | Extracts structured fields with Claude Haiku on Amazon Bedrock |
| Wrong AI output reaches the record | Confidence below 0.8 waits for a human review, and reviewers can correct fields |
| Bursts overwhelm downstream services | An SQS queue buffers uploads, and concurrency limits protect Bedrock and Textract quotas |
| Scanned PDFs and images can't be read directly | Async Textract extracts text, and a Step Functions workflow waits for the job without holding a Lambda open |
| Duplicate messages double-charge or double-notify | Conditional updates, deterministic execution names, and single-use task tokens make duplicates harmless |
| Bad files get retried over and over | Invalid input fails on the first attempt, and only infrastructure faults reach the dead-letter queue |
| One customer can see another's data | Separate API keys, tenant-scoped storage and queries, and per-tenant throttling and quotas |
| Customers have to poll for results | SNS notifications and HMAC-signed webhooks push each result to the customer's system |
| Auditors need a history of changes | A DynamoDB stream writes one audit row per status change, and S3 versioning keeps earlier file versions |
| Uploads can break the pipeline's limits | Presigned POST uploads go straight to S3, which enforces size and content-type limits |
| Secrets and settings are hard to change safely | Model settings and the webhook secret live in SSM, not in code |
| Deploys risk breaking production | CI runs tests and checks, then deploys with OIDC, with remote state and locking |

## Features

- **Multi-tenant:** each tenant has its own API key. Documents are stored under `tenant/<uuid>` in S3 and keyed by `(tenant_id, document_id)` in DynamoDB.
- **Uploads:** text, PDF, PNG, and JPEG, up to 10 MB, sent straight to S3 with a presigned POST.
- **Extraction:** text goes straight to the model. PDFs and images go through async Textract first.
- **Validation and review:** results below confidence 0.8 wait in `REVIEW` until a reviewer approves or rejects them, with optional corrections.
- **Audit trail:** one row per status change, from DynamoDB streams. The uploads bucket is versioned.
- **Notifications:** SNS for every terminal status, plus per-tenant signed webhooks with retries and a dead-letter queue.
- **Settings in SSM:** model ID, review threshold, and extraction prompt change without a redeploy.

## Document lifecycle

| Status | Meaning |
|--------|---------|
| `PROCESSING` | Claimed, workflow running |
| `COMPLETED` | Extracted with confidence 0.8 or higher, or approved in review |
| `REVIEW` | Extracted below 0.8, waiting for a reviewer |
| `FAILED` | Rejected as bad input, rejected in review, or failed unexpectedly. `last_error` says why. |

## Using the API

Each tenant gets a key. Get the key IDs and values from Terraform:

```bash
cd infra
URL=$(terraform output -raw api_invoke_url)
terraform output -json tenant_api_key_ids
KEY=$(aws apigateway get-api-key --api-key <KEY_ID> --include-value --query value --output text)
```

Request an upload, then post the file to the returned URL with the returned fields:

```bash
curl -s -X POST "$URL/documents" -H "x-api-key: $KEY" -H "Content-Type: application/json" \
  -d '{"content_type": "text/plain"}'
```

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/documents` | Create a presigned upload |
| `GET` | `/documents` | List your documents, optionally `?status=REVIEW` |
| `GET` | `/documents/{id}` | Read one document, including its extraction |
| `POST` | `/documents/{id}/review` | Approve or reject a document in `REVIEW` |

Review body:

```json
{"decision": "approve", "corrections": {"name": "John Okafor"}}
```

Unknown keys get `403`. Reviewing a document that isn't in `REVIEW` gets `409`.

## Tenant setup

- Tenants are listed in `infra/api.tf` (`locals.tenants`). Adding one creates its API key.
- Webhook settings are per tenant, in SSM. Set them with the CLI:

```bash
aws ssm put-parameter --name /docpipe/tenants/<tenant>/webhook_url --type String \
  --value "https://example.com/hook" --overwrite
aws ssm put-parameter --name /docpipe/tenants/<tenant>/webhook_secret --type SecureString \
  --value "<shared-secret>" --overwrite
```

Each request is signed with `X-DocPipe-Signature: sha256=` followed by HMAC-SHA256 of `"<timestamp>.<body>"`, using the tenant's secret. Check the timestamp too, and reject old requests. Only HTTPS URLs are used.

## Development

```bash
python -m pytest tests/ -v          # unit tests, no AWS access needed
cd infra && terraform fmt -recursive && terraform validate
```

Test layout:

| File | Covers |
|------|--------|
| `tests/test_api_handler.py` | Tenancy, routes, validation |
| `tests/test_review.py` | Review decisions and conflicts |
| `tests/test_starter.py` | Claiming documents and starting workflows |
| `tests/test_steps.py`, `tests/test_textract_steps.py` | Workflow steps |
| `tests/test_textract_callback.py` | Resuming workflows from Textract |
| `tests/test_stream_handler.py` | Audit rows and notifications |
| `tests/test_webhook.py` | Signing, skipping, and retries |
| `tests/test_config.py` | Reading settings from SSM |

`scripts/textract_smoke.py` runs async Textract against one S3 object, for manual checks.

## Deployment

Pushing to `main` runs CI:

1. Unit tests
2. `terraform fmt -check` and `terraform validate`
3. `terraform apply` through the `docpipe-github-deploy` role, using OIDC (no stored AWS keys)

Terraform state is in S3 (`docpipe-tfstate-<account>`) with locking. The deploy role's permissions come from the `docpipe-deploy` policy, which is limited to `docpipe-*` resources and the services this project uses.

## Operations

Alarms (all publish to `docpipe-alerts`): DLQ depth, work-queue age, starter errors, p99 duration, workflow failures, and workflow timeouts. The `docpipe` CloudWatch dashboard shows queue depth, invocations, errors, duration, and write capacity.

The runbook covers the DLQ, failed workflow executions, and common causes: [docs/dlq-runbook.md](docs/dlq-runbook.md).

## Repository layout

src/ Lambda code: starter, steps, API, stream, webhook, Textract callback
infra/ Terraform: API, queues, workflow, tables, audit, webhooks, config, alarms
tests/ Unit tests (run in CI)
scripts/ Manual test helpers
docs/ Architecture, project overview, runbook
.github/ CI workflow


## Known limitations

- The Lambda code hash differs between machines, so a local `terraform plan` always shows one in-place update on the worker. CI is the source of truth.
- API keys, not Cognito, authenticate clients.
- Multi-page PDFs with partial OCR success are rejected rather than sent to review.
- Webhook delivery is at least once. Receivers should dedupe on `event_id`.
