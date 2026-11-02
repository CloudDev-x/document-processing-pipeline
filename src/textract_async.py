import time

import boto3

textract = boto3.client("textract")

TERMINAL_STATUSES = {"SUCCEEDED", "FAILED", "PARTIAL_SUCCESS"}


def start_text_detection(bucket, key):
    resp = textract.start_document_text_detection(
        DocumentLocation={"S3Object": {"Bucket": bucket, "Name": key}}
    )
    return resp["JobId"]


def get_text_detection(job_id):
    """Return (status, text). Text is None until the job reaches a terminal status."""
    lines = []
    token = None
    while True:
        kwargs = {"JobId": job_id}
        if token:
            kwargs["NextToken"] = token
        resp = textract.get_document_text_detection(**kwargs)
        status = resp["JobStatus"]
        if status not in TERMINAL_STATUSES:
            return status, None
        if status == "FAILED":
            return status, resp.get("StatusMessage", "Textract job failed")
        lines.extend(b["Text"] for b in resp.get("Blocks", []) if b["BlockType"] == "LINE")
        token = resp.get("NextToken")
        if not token:
            return status, "\n".join(lines)


def wait_for_text(job_id, timeout_seconds=300, poll_seconds=5):
    """Polling helper for manual testing only. The pipeline uses an SNS callback instead."""
    deadline = time.monotonic() + timeout_seconds
    while True:
        status, text = get_text_detection(job_id)
        if status in TERMINAL_STATUSES:
            return status, text
        if time.monotonic() > deadline:
            raise TimeoutError(f"Textract job {job_id} still {status} after {timeout_seconds}s")
        time.sleep(poll_seconds)
