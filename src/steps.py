import json
import logging
import os
from datetime import datetime, timezone

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError, ConnectTimeoutError, ReadTimeoutError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

BUCKET_NAME = os.environ["BUCKET_NAME"]
MODEL_ID = os.environ["MODEL_ID"]
table = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])
s3 = boto3.client("s3")
# One attempt per call. Step Functions owns retries, so each attempt is visible in the workflow history.
bedrock = boto3.client(
    "bedrock-runtime",
    config=Config(read_timeout=30, connect_timeout=5, retries={"max_attempts": 1, "mode": "standard"}),
)

REVIEW_THRESHOLD = 0.8  # results below this confidence go to a human
ALLOWED_TYPES = {"statement", "id", "loan_form", "other"}
TRANSIENT_BEDROCK_ERRORS = {
    "ThrottlingException",
    "ServiceUnavailableException",
    "ModelTimeoutException",
    "InternalServerException",
}

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


def load_document(document_id):
    obj = s3.get_object(Bucket=BUCKET_NAME, Key=document_id)
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


def action_extract(event):
    return call_model(load_document(event["document_id"]))


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
