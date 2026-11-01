import json
import logging
import os
from datetime import datetime, timezone
from urllib.parse import unquote_plus

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

table = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])


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


def mark_completed(document_id):
    table.update_item(
        Key={"document_id": document_id},
        UpdateExpression="SET #s = :completed, updated_at = :now",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":completed": "COMPLETED", ":now": now_iso()},
    )


def record_error(document_id, message):
    table.update_item(
        Key={"document_id": document_id},
        UpdateExpression="SET last_error = :err, updated_at = :now",
        ExpressionAttributeValues={":err": message[:500], ":now": now_iso()},
    )


def process_document(document_id):
    if not claim(document_id):
        logger.info("Duplicate delivery, already completed: %s", document_id)
        return
    try:
        # Stub: the model step replaces this line in a later step.
        logger.info("Processing %s", document_id)
        mark_completed(document_id)
    except Exception as err:
        record_error(document_id, str(err))
        raise  # re-raise so SQS retries the message


def lambda_handler(event, context):
    for record in event["Records"]:
        body = json.loads(record["body"])
        if body.get("Event") == "s3:TestEvent":
            logger.info("Ignoring S3 test event")
            continue
        for s3_record in body["Records"]:
            # S3 URL-encodes object keys in events (spaces become "+"), so decode them.
            key = unquote_plus(s3_record["s3"]["object"]["key"])
            process_document(key)
