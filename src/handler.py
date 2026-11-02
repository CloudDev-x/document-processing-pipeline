import json
import logging
import os
import re
from datetime import datetime, timezone
from urllib.parse import unquote_plus

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

table = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])
sfn = boto3.client("stepfunctions")
STATE_MACHINE_ARN = os.environ["STATE_MACHINE_ARN"]


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def claim(document_id):
    """Mark a document PROCESSING unless it is already COMPLETED or FAILED. Returns False for duplicates."""
    try:
        table.update_item(
            Key={"document_id": document_id},
            UpdateExpression="SET #s = :processing, updated_at = :now ADD attempts :one",
            ConditionExpression="attribute_not_exists(document_id) OR (#s <> :completed AND #s <> :failed)",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":processing": "PROCESSING",
                ":completed": "COMPLETED",
                ":failed": "FAILED",
                ":now": now_iso(),
                ":one": 1,
            },
        )
        return True
    except ClientError as err:
        if err.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def execution_name(document_id, receive_count):
    # Execution names allow letters, digits, hyphens and underscores, up to 80 characters.
    # The same S3 event (same receive count) maps to the same name, so Step Functions rejects duplicates.
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", document_id)
    return f"{safe}-{receive_count}"[:80]


def start_workflow(document_id, receive_count):
    try:
        sfn.start_execution(
            stateMachineArn=STATE_MACHINE_ARN,
            name=execution_name(document_id, receive_count),
            input=json.dumps({"document_id": document_id}),
        )
    except ClientError as err:
        if err.response["Error"]["Code"] == "ExecutionAlreadyExists":
            logger.info("Workflow already started for %s", document_id)
            return
        raise


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
            if not claim(key):
                logger.info("Duplicate delivery, already completed or failed: %s", key)
                continue
            start_workflow(key, receive_count)
