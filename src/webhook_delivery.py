import hashlib
import hmac
import json
import logging
import time
import urllib.request

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ssm = boto3.client("ssm")
DISABLED = "disabled"
TIMEOUT_SECONDS = 5


def tenant_settings(tenant_id):
    names = [
        f"/docpipe/tenants/{tenant_id}/webhook_url",
        f"/docpipe/tenants/{tenant_id}/webhook_secret",
    ]
    resp = ssm.get_parameters(Names=names, WithDecryption=True)
    values = {p["Name"]: p["Value"] for p in resp["Parameters"]}
    return values.get(names[0], DISABLED), values.get(names[1], DISABLED)


def sign(secret, timestamp, body):
    mac = hmac.new(secret.encode("utf-8"), f"{timestamp}.{body}".encode("utf-8"), hashlib.sha256)
    return "sha256=" + mac.hexdigest()


def build_payload(message):
    event_id = f"{message['document_id']}:{message['status']}:{message.get('updated_at', '')}"
    payload = {
        "event_id": event_id,
        "tenant_id": message["tenant_id"],
        "document_id": message["document_id"],
        "status": message["status"],
        "updated_at": message.get("updated_at", ""),
    }
    if message.get("last_error"):
        payload["last_error"] = message["last_error"]
    return event_id, json.dumps(payload, separators=(",", ":"), sort_keys=True)


def deliver(message):
    url, secret = tenant_settings(message["tenant_id"])
    if url == DISABLED or secret == DISABLED:
        return "skipped: webhook not configured"
    if not url.startswith("https://"):
        logger.error("Webhook URL for %s is not HTTPS; not sending", message["tenant_id"])
        return "skipped: webhook URL is not HTTPS"

    event_id, body = build_payload(message)
    timestamp = str(int(time.time()))
    request = urllib.request.Request(
        url,
        data=body.encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-DocPipe-Event-Id": event_id,
            "X-DocPipe-Timestamp": timestamp,
            "X-DocPipe-Signature": sign(secret, timestamp, body),
        },
    )
    # A non-2xx response raises HTTPError. Any exception propagates, so SQS retries the delivery.
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as resp:
        if not 200 <= resp.status < 300:
            raise RuntimeError(f"webhook returned {resp.status}")
    return "delivered"


def lambda_handler(event, context):
    results = []
    for record in event["Records"]:
        envelope = json.loads(record["body"])
        message = json.loads(envelope["Message"])
        results.append(deliver(message))
    return {"results": results}
