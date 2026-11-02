"""Unit tests for the DynamoDB stream handler: audit rows and terminal-status notifications. AWS calls are mocked."""

import os
from unittest.mock import patch

os.environ.setdefault("AUDIT_TABLE_NAME", "test-audit")
os.environ.setdefault("EVENTS_TOPIC_ARN", "arn:aws:sns:us-east-1:123456789012:test")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

import pytest
from botocore.exceptions import ClientError

from src import stream_handler


def image(status, updated_at="2026-11-02T10:00:00+00:00", last_error=None):
    item = {
        "tenant_id": {"S": "acme"},
        "document_id": {"S": "doc-1"},
        "status": {"S": status},
        "updated_at": {"S": updated_at},
    }
    if last_error:
        item["last_error"] = {"S": last_error}
    return item


def record(event_name, new=None, old=None, event_id="e1"):
    dynamodb = {}
    if new is not None:
        dynamodb["NewImage"] = new
    if old is not None:
        dynamodb["OldImage"] = old
    return {"eventName": event_name, "eventID": event_id, "dynamodb": dynamodb}


@pytest.fixture
def audit():
    with patch.object(stream_handler, "AUDIT_TABLE") as mock:
        yield mock


@pytest.fixture
def sns():
    with patch.object(stream_handler, "sns") as mock:
        yield mock


def test_new_document_writes_audit_row_without_notifying(audit, sns):
    event = {"Records": [record("INSERT", new=image("PROCESSING"))]}
    stream_handler.lambda_handler(event, None)
    item = audit.put_item.call_args.kwargs["Item"]
    assert item["pk"] == "acme#doc-1"
    assert item["from_status"] == "NEW"
    assert item["to_status"] == "PROCESSING"
    sns.publish.assert_not_called()


def test_terminal_status_notifies_and_records(audit, sns):
    event = {"Records": [record("MODIFY", new=image("COMPLETED"), old=image("PROCESSING"))]}
    stream_handler.lambda_handler(event, None)
    kwargs = sns.publish.call_args.kwargs
    assert '"status": "COMPLETED"' in kwargs["Message"]
    assert kwargs["MessageAttributes"]["tenant_id"]["StringValue"] == "acme"
    assert audit.put_item.call_args.kwargs["Item"]["from_status"] == "PROCESSING"


def test_failed_notification_includes_last_error(audit, sns):
    new = image("FAILED", last_error="TerminalError: bad input")
    stream_handler.lambda_handler({"Records": [record("MODIFY", new=new, old=image("PROCESSING"))]}, None)
    assert "bad input" in sns.publish.call_args.kwargs["Message"]


def test_no_status_change_is_skipped(audit, sns):
    event = {"Records": [record("MODIFY", new=image("PROCESSING", "2026-11-02T10:01:00+00:00"), old=image("PROCESSING"))]}
    stream_handler.lambda_handler(event, None)
    audit.put_item.assert_not_called()
    sns.publish.assert_not_called()


def test_removed_records_are_skipped(audit, sns):
    stream_handler.lambda_handler({"Records": [record("REMOVE", old=image("COMPLETED"))]}, None)
    audit.put_item.assert_not_called()


def test_duplicate_audit_row_is_not_an_error(audit, sns):
    audit.put_item.side_effect = ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": "exists"}}, "PutItem"
    )
    event = {"Records": [record("MODIFY", new=image("COMPLETED"), old=image("PROCESSING"))]}
    result = stream_handler.lambda_handler(event, None)
    assert result == {"processed": 1}
