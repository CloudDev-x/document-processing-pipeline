import json
import logging
import os
from collections import Counter

import boto3
from boto3.dynamodb.types import TypeDeserializer
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

AUDIT_TABLE = boto3.resource("dynamodb").Table(os.environ["AUDIT_TABLE_NAME"])
EVENTS_TOPIC_ARN = os.environ["EVENTS_TOPIC_ARN"]
sns = boto3.client("sns")
deserializer = TypeDeserializer()
TERMINAL_STATUSES = {"COMPLETED", "REVIEW", "FAILED"}


def plain(image):
    return {k: deserializer.deserialize(v) for k, v in (image or {}).items()}


def publish(new):
    tenant_id = new["tenant_id"]
    status = new["status"]
    message = {
        "tenant_id": tenant_id,
        "document_id": new["document_id"],
        "status": status,
        "updated_at": new.get("updated_at", ""),
    }
    if new.get("last_error"):
        message["last_error"] = new["last_error"]
    sns.publish(
        TopicArn=EVENTS_TOPIC_ARN,
        Message=json.dumps(message),
        MessageAttributes={
            "tenant_id": {"DataType": "String", "StringValue": tenant_id},
            "status": {"DataType": "String", "StringValue": status},
        },
    )


def record_audit(event_id, new, old):
    AUDIT_TABLE.put_item(
        Item={
            "pk": f"{new['tenant_id']}#{new['document_id']}",
            "sk": f"{new.get('updated_at', '')}#{event_id}",
            "from_status": old.get("status", "NEW"),
            "to_status": new.get("status", ""),
            "last_error": new.get("last_error", ""),
            "source_event": event_id,
        },
        ConditionExpression="attribute_not_exists(sk)",
    )


def handle_record(record):
    if record["eventName"] not in {"INSERT", "MODIFY"}:
        return "skipped"
    images = record["dynamodb"]
    new = plain(images.get("NewImage"))
    old = plain(images.get("OldImage"))
    if new.get("status") == old.get("status"):
        return "no status change"

    # Publish first, then record the audit row. A retry can therefore repeat a notification,
    # but it can never lose one. Subscribers should dedupe on document_id and status.
    if new.get("status") in TERMINAL_STATUSES:
        publish(new)
    try:
        record_audit(record["eventID"], new, old)
    except ClientError as err:
        if err.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return "audit already recorded"
        raise
    return "recorded"


def lambda_handler(event, context):
    results = Counter(handle_record(record) for record in event["Records"])
    logger.info("Processed %d stream records: %s", len(event["Records"]), dict(results))
    return {"processed": len(event["Records"])}
