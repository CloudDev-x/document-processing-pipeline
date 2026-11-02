import json
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError

BUCKET_NAME = os.environ["BUCKET_NAME"]
TABLE_NAME = os.environ["TABLE_NAME"]
TENANT_BY_KEY_ID = json.loads(os.environ["TENANT_BY_KEY_ID"])
UPLOAD_TTL_SECONDS = 300
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
ALLOWED_CONTENT_TYPES = {"text/plain", "application/pdf", "image/png", "image/jpeg"}
REVIEW_DECISIONS = {"approve": "COMPLETED", "reject": "FAILED"}
MAX_CORRECTIONS = 50

s3 = boto3.client("s3")
table = boto3.resource("dynamodb").Table(TABLE_NAME)


def _json_default(value):
    if isinstance(value, Decimal):
        return int(value) if value % 1 == 0 else float(value)
    raise TypeError(f"not serializable: {type(value)}")


def respond(status, body):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, default=_json_default),
    }


def tenant_for(event):
    identity = (event.get("requestContext") or {}).get("identity") or {}
    return TENANT_BY_KEY_ID.get(identity.get("apiKeyId"))


def reviewer_for(event):
    identity = (event.get("requestContext") or {}).get("identity") or {}
    return identity.get("apiKeyId", "unknown")


def create_upload(tenant_id, event):
    try:
        payload = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return respond(400, {"error": "body must be JSON"})

    content_type = payload.get("content_type", "text/plain")
    if content_type not in ALLOWED_CONTENT_TYPES:
        return respond(400, {"error": f"unsupported content_type: {content_type}"})

    document_id = str(uuid.uuid4())
    upload = s3.generate_presigned_post(
        Bucket=BUCKET_NAME,
        Key=f"{tenant_id}/{document_id}",
        Fields={"Content-Type": content_type},
        Conditions=[
            {"Content-Type": content_type},
            ["content-length-range", 1, MAX_UPLOAD_BYTES],
        ],
        ExpiresIn=UPLOAD_TTL_SECONDS,
    )
    return respond(201, {"document_id": document_id, "upload": upload, "expires_in": UPLOAD_TTL_SECONDS})


def get_document(tenant_id, document_id):
    item = table.get_item(Key={"tenant_id": tenant_id, "document_id": document_id}).get("Item")
    if item is None:
        return respond(404, {"error": "not found"})
    if isinstance(item.get("extraction"), str):
        item["extraction"] = json.loads(item["extraction"])
    return respond(200, item)


def list_documents(tenant_id, event):
    status = (event.get("queryStringParameters") or {}).get("status")
    query_args = {
        "KeyConditionExpression": "tenant_id = :t",
        "ExpressionAttributeNames": {"#s": "status"},
        "ExpressionAttributeValues": {":t": tenant_id},
        "ProjectionExpression": "document_id, #s, updated_at, attempts",
    }
    if status:
        query_args["FilterExpression"] = "#s = :status"
        query_args["ExpressionAttributeValues"][":status"] = status
    items = table.query(**query_args).get("Items", [])
    return respond(200, {"documents": items, "count": len(items)})


def review_document(tenant_id, document_id, event):
    try:
        payload = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return respond(400, {"error": "body must be JSON"})

    decision = payload.get("decision")
    if decision not in REVIEW_DECISIONS:
        return respond(400, {"error": "decision must be approve or reject"})

    corrections = payload.get("corrections", {})
    if (
        not isinstance(corrections, dict)
        or len(corrections) > MAX_CORRECTIONS
        or not all(isinstance(k, str) and isinstance(v, str) for k, v in corrections.items())
    ):
        return respond(400, {"error": f"corrections must be an object of up to {MAX_CORRECTIONS} string values"})

    key = {"tenant_id": tenant_id, "document_id": document_id}
    item = table.get_item(Key=key).get("Item")
    if item is None:
        return respond(404, {"error": "not found"})
    if item.get("status") != "REVIEW":
        return respond(409, {"error": f"document is {item.get('status')}, not awaiting review"})

    new_status = REVIEW_DECISIONS[decision]
    now = datetime.now(timezone.utc).isoformat()
    update_expression = (
        "SET #s = :new, review_decision = :decision, reviewed_by = :by, "
        "reviewed_at = :at, updated_at = :at, corrections = :corrections"
    )
    values = {
        ":review": "REVIEW",
        ":new": new_status,
        ":decision": decision,
        ":by": reviewer_for(event),
        ":at": now,
        ":corrections": corrections,
    }
    if decision == "reject":
        update_expression += ", last_error = :err"
        values[":err"] = "rejected in review"

    try:
        # The condition stops two reviewers acting on the same document at once.
        table.update_item(
            Key=key,
            UpdateExpression=update_expression,
            ConditionExpression="#s = :review",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues=values,
        )
    except ClientError as err:
        if err.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return respond(409, {"error": "document status changed during review"})
        raise
    return respond(200, {"document_id": document_id, "status": new_status})


def lambda_handler(event, context):
    tenant_id = tenant_for(event)
    if tenant_id is None:
        return respond(403, {"error": "forbidden"})

    route = event.get("resource")
    method = event.get("httpMethod")

    if route == "/documents" and method == "POST":
        return create_upload(tenant_id, event)
    if route == "/documents" and method == "GET":
        return list_documents(tenant_id, event)
    if route == "/documents/{id}" and method == "GET":
        return get_document(tenant_id, event["pathParameters"]["id"])
    if route == "/documents/{id}/review" and method == "POST":
        return review_document(tenant_id, event["pathParameters"]["id"], event)
    return respond(405, {"error": "method not allowed"})
