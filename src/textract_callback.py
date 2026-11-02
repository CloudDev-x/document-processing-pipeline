import json
import os

import boto3
from botocore.exceptions import ClientError

table = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])
sfn = boto3.client("stepfunctions")


def handle_message(message):
    job_id = message.get("JobId")
    status = message.get("Status")
    # The job tag is tenant_id:document_id, set when the workflow started the job.
    job_tag = message.get("JobTag") or ""
    if ":" not in job_tag:
        return "ignored: no tenant-scoped JobTag"
    tenant_id, document_id = job_tag.split(":", 1)
    key = {"tenant_id": tenant_id, "document_id": document_id}

    item = table.get_item(Key=key).get("Item") or {}
    token = item.get("task_token")
    if not token:
        return "ignored: no pending task"

    # Claim the token before resuming the workflow. A duplicate SNS delivery finds no token and stops here.
    # If the resume call fails after this point, the workflow's own timeout fails it cleanly.
    try:
        table.update_item(
            Key=key,
            UpdateExpression="REMOVE task_token",
            ConditionExpression="task_token = :token",
            ExpressionAttributeValues={":token": token},
        )
    except ClientError as err:
        if err.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return "ignored: already claimed"
        raise

    if status == "SUCCEEDED":
        sfn.send_task_success(
            taskToken=token,
            output=json.dumps({"document_id": document_id, "textract_job_id": job_id}),
        )
        return "resumed"

    sfn.send_task_failure(
        taskToken=token,
        error="TextractJobFailed",
        cause=f"Textract status: {status}",
    )
    return "failed"


def lambda_handler(event, context):
    results = []
    for record in event["Records"]:
        message = json.loads(record["Sns"]["Message"])
        results.append(handle_message(message))
    return {"results": results}
