import json
import os
import uuid
from decimal import Decimal

import boto3

BUCKET_NAME = os.environ["BUCKET_NAME"]
TABLE_NAME = os.environ["TABLE_NAME"]
UPLOAD_TTL_SECONDS = 300
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
ALLOWED_CONTENT_TYPES = {"text/plain"}

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


def create_upload(event):
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
        Key=document_id,
        Fields={"Content-Type": content_type},
        Conditions=[
            {"Content-Type": content_type},
            ["content-length-range", 1, MAX_UPLOAD_BYTES],
        ],
        ExpiresIn=UPLOAD_TTL_SECONDS,
    )
    return respond(201, {"document_id": document_id, "upload": upload, "expires_in": UPLOAD_TTL_SECONDS})


def get_document(document_id):
    item = table.get_item(Key={"document_id": document_id}).get("Item")
    if item is None:
        return respond(404, {"error": "not found"})
    if isinstance(item.get("extraction"), str):
        item["extraction"] = json.loads(item["extraction"])
    return respond(200, item)


def list_documents(event):
    status = (event.get("queryStringParameters") or {}).get("status")
    scan_args = {
        "ProjectionExpression": "document_id, #s, updated_at, attempts",
        "ExpressionAttributeNames": {"#s": "status"},
    }
    if status:
        scan_args["FilterExpression"] = "#s = :status"
        scan_args["ExpressionAttributeValues"] = {":status": status}
    items = table.scan(**scan_args).get("Items", [])
    return respond(200, {"documents": items, "count": len(items)})


def lambda_handler(event, context):
    route = event.get("resource")
    method = event.get("httpMethod")

    if route == "/documents" and method == "POST":
        return create_upload(event)
    if route == "/documents" and method == "GET":
        return list_documents(event)
    if route == "/documents/{id}" and method == "GET":
        return get_document(event["pathParameters"]["id"])
    return respond(405, {"error": "method not allowed"})
