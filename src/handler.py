import json
import logging
import os
from datetime import datetime, timezone
from urllib.parse import unquote_plus

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

BUCKET_NAME = os.environ["BUCKET_NAME"]
MODEL_ID = os.environ["MODEL_ID"]
table = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])
s3 = boto3.client("s3")
# One attempt per invocation. Retries are handled by SQS, so they stay visible in the queue metrics.
bedrock = boto3.client(
    "bedrock-runtime",
    config=Config(read_timeout=30, connect_timeout=5, retries={"max_attempts": 1, "mode": "standard"}),
)

MAX_ATTEMPTS = 3  # must match maxReceiveCount on the work queue
REVIEW_THRESHOLD = 0.8  # results below this confidence go to a human
ALLOWED_TYPES = {"statement", "id", "loan_form", "other"}

EXTRACTION_PROMPT = """You classify and extract data from a customer document.
Return only a JSON object with these keys:
"document_type": one of "statement", "id", "loan_form", "other"
"confidence": a number from 0 to 1
"fields": an object of key fields you can read from the document, with string values
Do not include any other text."""


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def claim(document_id):
    """Mark a document PROCESSING unless it is already COMPLETED. Returns False for duplicates."""
    try:
        table.update_item(
            Key={"document_id": document_id},
            UpdateExpression="SET #s = :processing, updated_at = :now ADD attempts :one",
            ConditionExpression="attribute_not_exists(document_id) OR #s <> :completed",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":processing": "PROCESSING",
                ":completed": "COMPLETED",
                ":now": now_iso(),
                ":one": 1,
            },
        )
        return True
    except ClientError as err:
        if err.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def load_document(document_id):
    obj = s3.get_object(Bucket=BUCKET_NAME, Key=document_id)
    try:
        return obj["Body"].read().decode("utf-8")
    except UnicodeDecodeError as err:
        raise ValueError("Only UTF-8 text documents are supported at this stage") from err


def extract(document_text):
    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 1024,
        "system": EXTRACTION_PROMPT,
        "messages": [{"role": "user", "content": document_text}],
    }
    response = bedrock.invoke_model(modelId=MODEL_ID, body=json.dumps(body))
    payload = json.loads(response["body"].read())
    return payload["content"][0]["text"], payload["usage"]


def parse_model_json(text):
    """Pull the JSON object out of the reply, even if it is wrapped in a code block or prose."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        logger.warning("Model reply contained no JSON object: %r", text[:500])
        raise ValueError("Model reply contained no JSON object")
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError as err:
        logger.warning("Model reply was not valid JSON: %r", text[:500])
        raise ValueError("Model reply was not valid JSON") from err


def validate_result(result):
    if not isinstance(result, dict):
        raise ValueError("Model output is not a JSON object")
    if result.get("document_type") not in ALLOWED_TYPES:
        raise ValueError(f"Invalid document_type: {result.get('document_type')!r}")
    confidence = result.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        raise ValueError("confidence must be a number from 0 to 1")
    fields = result.get("fields")
    if not isinstance(fields, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in fields.items()
    ):
        raise ValueError("fields must be an object of string values")
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


def record_failure(document_id, message, final):
    if final:
        # Last attempt: operators should see FAILED, not PROCESSING.
        table.update_item(
            Key={"document_id": document_id},
            UpdateExpression="SET #s = :failed, last_error = :err, updated_at = :now",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":failed": "FAILED", ":err": message[:500], ":now": now_iso()},
        )
    else:
        table.update_item(
            Key={"document_id": document_id},
            UpdateExpression="SET last_error = :err, updated_at = :now",
            ExpressionAttributeValues={":err": message[:500], ":now": now_iso()},
        )


def process_document(document_id, receive_count):
    if not claim(document_id):
        logger.info("Duplicate delivery, already completed: %s", document_id)
        return
    try:
        text = load_document(document_id)
        raw_reply, usage = extract(text)
        result = validate_result(parse_model_json(raw_reply))
        status = "REVIEW" if result["confidence"] < REVIEW_THRESHOLD else "COMPLETED"
        finalize(document_id, status, result, usage)
        logger.info("Finished %s with status %s", document_id, status)
    except Exception as err:
        record_failure(document_id, str(err), final=receive_count >= MAX_ATTEMPTS)
        raise  # re-raise so SQS retries the message, then moves it to the DLQ


def lambda_handler(event, context):
    for record in event["Records"]:
        receive_count = int(record["attributes"]["ApproximateReceiveCount"])
        body = json.loads(record["body"])
        if body.get("Event") == "s3:TestEvent":
            logger.info("Ignoring S3 test event")
            continue
        for s3_record in body["Records"]:
            # S3 URL-encodes object keys in events (spaces become "+"), so decode them.
            key = unquote_plus(s3_record["s3"]["object"]["key"])
            process_document(key, receive_count)
