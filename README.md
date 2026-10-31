# document-processing-pipeline

A serverless document processing pipeline on AWS. Customers upload documents such as statements, IDs, and loan forms. Each document is processed asynchronously, is never lost, and is tracked to completion.

> Status: in progress. Built by hand, one component at a time. Synthetic documents only; no real customer data.

## Problem

Uploads arrive at unpredictable volumes. Processing must be reliable and visible. Failures must be retried where sensible, isolated so they don't block other documents, and routed to a dead letter queue (DLQ) when they cannot be processed.

## Design principle

The pipeline owns reliability. Intake, queuing, retries, the DLQ, and status tracking are deterministic infrastructure. The AI model is one worker inside the processing step. It is never trusted to manage state, retries, or delivery.

## Architecture

    Customer --> presigned URL --> S3 (uploads)
                                      |
                                 S3 event
                                      |
                                      v
                              SQS (work queue) --> DLQ (after max attempts)
                                      |
                                      v
                           Lambda (processing worker)
                              |                |
                              v                v
                     Model call (hard      DynamoDB (document status)
                     timeout, schema
                     validation)

Status flow: RECEIVED -> PROCESSING -> COMPLETED | REVIEW | FAILED

## Reliability targets

- No document is lost. Messages stay in SQS until processed or moved to the DLQ.
- Duplicate deliveries produce one result, keyed on document ID.
- Each model call has a hard timeout. Output must pass schema validation before it is accepted.
- Low-confidence or invalid extraction goes to REVIEW, never COMPLETED.
- Repeated failures go to the DLQ, with an alarm on DLQ depth.

## Planned layout

    document-processing-pipeline/
      infra/      Terraform
      src/        Lambda processing code
      schemas/    model output schemas
      tests/
      docs/       architecture and runbook

## Runbook: DLQ handling

To be completed as the project is built. Will cover inspecting failed messages, fixing the cause, and redriving.

## Acceptance criteria

- 1,000 uploads all reach a terminal or REVIEW status, with none lost.
- A model call that times out is retried, then sent to the DLQ.
- Invalid model output is never written as COMPLETED.
- A duplicated SQS message produces one result.
- A model outage leaves documents waiting in the queue, and they complete after recovery.
- An injected failure triggers the DLQ alarm.

## Progress

- [ ] Upload path (S3 presigned URL, S3 event to SQS)
- [ ] Processing worker (Lambda)
- [ ] Model call with timeout and validation
- [ ] DynamoDB status tracking
- [ ] DLQ and alarms
- [ ] Terraform
- [ ] Load test
