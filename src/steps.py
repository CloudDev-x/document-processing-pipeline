import json
import logging
import os
import re
import time
from datetime import datetime, timezone

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError, ConnectTimeoutError, ReadTimeoutError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

BUCKET_NAME = os.environ["BUCKET_NAME"]
MODEL_ID = os.environ["MODEL_ID"]
SCRATCH_BUCKET_NAME = os.environ.get("SCRATCH_BUCKET_NAME", "")
TEXTRACT_TOPIC_ARN = os.environ.get("TEXTRACT_TOPIC_ARN", "")
TEXTRACT_ROLE_ARN = os.environ.get("TEXTRACT_ROLE_ARN", "")
table = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])
s3 = boto3.client("s3")
textract = boto3.client("textract")
# One attempt per call. Step Functions owns retries, so each attempt is visible in the workflow history.
bedrock = boto3.client(
    "bedrock-runtime",
    config=Config(read_timeout=30, connect_timeout=5, retries={"max_attempts": 1, "mode": "standard"}),
)

REVIEW_THRESHOLD = 0.8  # results below this confidence go to a human
ALLOWED_TYPES = {"statement", "id", "loan_form", "other"}
TEXT_CONTENT_TYPES = {"text/plain"}
TEXTRACT_CONTENT_TYPES = {"application/pdf", "image/png", "image/jpeg"}
TRANSIENT_BEDROCK_ERRORS = {
    "ThrottlingException",
    "ServiceUnavailableException",
    "ModelTimeoutException",
    "InternalServerException",
}
TEXTRACT_TERMINAL_STATUSES = {"SUCCEEDED", "FAILED", "PARTIAL_SUCCESS"}
# Textract JobTag rules: letters, digits and a few symbols, up to 64 characters.
JOB_TAG_PATTERN = re.compile(r"^[A-Za-z0-9_.\-:+=@]{1,64}$")

EXTRACTION_PROMPT = """You classify and extract data from a customer document.
Return only a JSON object with these keys:
"document_type": one of "statement", "id", "loan_form", "other"
"confidence": a number from 0 to 1
"fields": an object of key fields you can read from the document, with string values
Do not include any other text."""


class TransientError(Exception):
    """Retried by the state machine with backoff. The Lambda error type must be exactly this name."""


class TerminalError(ValueError):
    """Bad input or bad model output. Marks the document FAILED without retrying."""


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def read_text(bucket, key):
    obj = s3.get_object(Bucket=bucket, Key=key)
    try:
        return obj["Body"].read().decode("utf-8")
    except UnicodeDecodeError as err:
        raise TerminalError("Only UTF-8 text documents are supported at this stage") from err


def call_model(document_text):
    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 1024,
        "system": EXTRACTION_PROMPT,
        "messages": [{"role": "user", "content": document_text}],
    }
    try:
        response = bedrock.invoke_model(modelId=MODEL_ID, body=json.dumps(body))
    except (ReadTimeoutError, ConnectTimeoutError) as err:
        raise TransientError(str(err)) from err
    except ClientError as err:
        if err.response["Error"]["Code"] in TRANSIENT_BEDROCK_ERRORS:
            raise TransientError(str(err)) from err
        raise
    payload = json.loads(response["body"].read())
    return {
        "raw_reply": payload["content"][0]["text"],
        "usage": {
            "input_tokens": payload["usage"]["input_tokens"],
            "output_tokens": payload["usage"]["output_tokens"],
        },
    }


def parse_model_json(text):
    """Pull the JSON object out of the reply, even if it is wrapped in a code block or prose."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        logger.warning("Model reply contained no JSON object: %r", text[:500])
        raise TerminalError("Model reply contained no JSON object")
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError as err:
        logger.warning("Model reply was not valid JSON: %r", text[:500])
        raise TerminalError("Model reply was not valid JSON") from err


def validate_result(result):
    if not isinstance(result, dict):
        raise TerminalError("Model output is not a JSON object")
    if result.get("document_type") not in ALLOWED_TYPES:
        raise TerminalError(f"Invalid document_type: {result.get('document_type')!r}")
    confidence = result.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        raise TerminalError("confidence must be a number from 0 to 1")
    fields = result.get("fields")
    if not isinstance(fields, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in fields.items()
    ):
        raise TerminalError("fields must be an object of string values")
    return result


def finalize(document_id, status, result, usage):
    table.update_item(
        Key={"document_id": document_id},
        UpdateExpression=(
            "SET #s = :status, extraction = :e, model_id = :m, "
            "input_tokens = :i, output_tokens = :o, updated_at = :now"
        ),
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":status": status,
            ":e": json.dumps(result),  # stored as a string to avoid DynamoDB float issues
            ":m": MODEL_ID,
            ":i": usage["input_tokens"],
            ":o": usage["output_tokens"],
            ":now": now_iso(),
        },
    )


def record_failure(document_id, message):
    table.update_item(
        Key={"document_id": document_id},
        UpdateExpression="SET #s = :failed, last_error = :err, updated_at = :now",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":failed": "FAILED", ":err": message[:500], ":now": now_iso()},
    )


def start_text_detection(bucket, key, job_tag=None, topic_arn=None, role_arn=None):
    kwargs = {"DocumentLocation": {"S3Object": {"Bucket": bucket, "Name": key}}}
    if job_tag:
        kwargs["JobTag"] = job_tag
    if topic_arn and role_arn:
        kwargs["NotificationChannel"] = {"SNSTopicArn": topic_arn, "RoleArn": role_arn}
    return textract.start_document_text_detection(**kwargs)["JobId"]


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
        if status not in TEXTRACT_TERMINAL_STATUSES:
            return status, None
        if status == "FAILED":
            return status, resp.get("StatusMessage", "Textract job failed")
        lines.extend(b["Text"] for b in resp.get("Blocks", []) if b["BlockType"] == "LINE")
        token = resp.get("NextToken")
        if not token:
            return status, "\n".join(lines)


def wait_for_text(job_id, timeout_seconds=300, poll_seconds=5):
    """Polling helper for manual testing only. The workflow uses an SNS callback instead."""
    deadline = time.monotonic() + timeout_seconds
    while True:
        status, text = get_text_detection(job_id)
        if status in TEXTRACT_TERMINAL_STATUSES:
            return status, text
        if time.monotonic() > deadline:
            raise TimeoutError(f"Textract job {job_id} still {status} after {timeout_seconds}s")
        time.sleep(poll_seconds)


def action_classify(event):
    head = s3.head_object(Bucket=BUCKET_NAME, Key=event["document_id"])
    content_type = head.get("ContentType", "").split(";")[0].strip().lower()
    if content_type in TEXT_CONTENT_TYPES:
        route = "text"
    elif content_type in TEXTRACT_CONTENT_TYPES:
        route = "textract"
    else:
        raise TerminalError(f"unsupported content type: {content_type or 'none'}")
    return {"route": route, "content_type": content_type}


def action_start_textract(event):
    document_id = event["document_id"]
    if not JOB_TAG_PATTERN.match(document_id):
        raise TerminalError("document id cannot be used as a Textract job tag")
    # Save the task token before starting the job. If the job finished first, the callback would find no token to resume.
    table.update_item(
        Key={"document_id": document_id},
        UpdateExpression="SET task_token = :t, updated_at = :now",
        ExpressionAttributeValues={":t": event["task_token"], ":now": now_iso()},
    )
    job_id = start_text_detection(
        BUCKET_NAME,
        document_id,
        job_tag=document_id,
        topic_arn=TEXTRACT_TOPIC_ARN,
        role_arn=TEXTRACT_ROLE_ARN,
    )
    table.update_item(
        Key={"document_id": document_id},
        UpdateExpression="SET textract_job_id = :j",
        ExpressionAttributeValues={":j": job_id},
    )
    return {"job_id": job_id}


def action_read_textract(event):
    document_id = event["document_id"]
    item = table.get_item(Key={"document_id": document_id}).get("Item") or {}
    job_id = item.get("textract_job_id")
    if not job_id:
        raise TerminalError("no Textract job recorded for this document")
    status, text = get_text_detection(job_id)
    if status not in TEXTRACT_TERMINAL_STATUSES:
        raise TransientError(f"Textract job still {status}")
    if status != "SUCCEEDED":
        raise TerminalError(f"Textract finished with status {status}")
    if not text or not text.strip():
        raise TerminalError("Textract found no text in the document")
    text_key = f"text/{document_id}.txt"
    s3.put_object(Bucket=SCRATCH_BUCKET_NAME, Key=text_key, Body=text.encode("utf-8"))
    return {"text_key": text_key}


def action_extract(event):
    if event.get("text_key"):
        text = read_text(SCRATCH_BUCKET_NAME, event["text_key"])
    else:
        text = read_text(BUCKET_NAME, event["document_id"])
    return call_model(text)


def action_validate(event):
    result = validate_result(parse_model_json(event["raw_reply"]))
    status = "REVIEW" if result["confidence"] < REVIEW_THRESHOLD else "COMPLETED"
    return {"status": status, "result": result}


def action_persist(event):
    finalize(event["document_id"], event["status"], event["result"], event["usage"])
    return {"status": event["status"]}


def action_fail(event):
    record_failure(event["document_id"], str(event.get("error", "unknown error")))
    return {"status": "FAILED"}


ACTIONS = {
    "classify": action_classify,
    "start_textract": action_start_textract,
    "read_textract": action_read_textract,
    "extract": action_extract,
    "validate": action_validate,
    "persist": action_persist,
    "fail": action_fail,
}


def lambda_handler(event, context):
    action = event["action"]
    if action not in ACTIONS:
        raise ValueError(f"unknown action: {action}")
    return ACTIONS[action](event)
