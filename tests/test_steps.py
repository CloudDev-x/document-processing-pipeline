"""Unit tests for the workflow step actions. AWS calls are mocked."""

import json
import os
from io import BytesIO
from unittest.mock import MagicMock, patch

os.environ.setdefault("BUCKET_NAME", "test-bucket")
os.environ.setdefault("TABLE_NAME", "test-table")
os.environ.setdefault("MODEL_ID", "test-model")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

import pytest
from botocore.exceptions import ClientError

from src import steps


def bedrock_reply(text, input_tokens=10, output_tokens=5):
    body = MagicMock()
    body.read.return_value = json.dumps(
        {"content": [{"text": text}], "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens}}
    ).encode()
    return {"body": body}


def client_error(code):
    return ClientError({"Error": {"Code": code, "Message": "x"}}, "InvokeModel")


@pytest.fixture
def s3():
    with patch.object(steps, "s3") as mock:
        yield mock


@pytest.fixture
def bedrock():
    with patch.object(steps, "bedrock") as mock:
        yield mock


@pytest.fixture
def table():
    with patch.object(steps, "table") as mock:
        yield mock


class TestExtract:
    def test_returns_reply_and_usage(self, s3, bedrock):
        s3.get_object.return_value = {"Body": BytesIO(b"Account statement")}
        bedrock.invoke_model.return_value = bedrock_reply('{"document_type":"statement"}', 12, 7)
        result = steps.lambda_handler({"action": "extract", "document_id": "doc-1"}, None)
        assert result == {
            "raw_reply": '{"document_type":"statement"}',
            "usage": {"input_tokens": 12, "output_tokens": 7},
        }
        s3.get_object.assert_called_once_with(Bucket="test-bucket", Key="doc-1")

    def test_throttling_is_transient(self, s3, bedrock):
        s3.get_object.return_value = {"Body": BytesIO(b"text")}
        bedrock.invoke_model.side_effect = client_error("ThrottlingException")
        with pytest.raises(steps.TransientError):
            steps.lambda_handler({"action": "extract", "document_id": "doc-1"}, None)

    def test_access_denied_is_not_transient(self, s3, bedrock):
        s3.get_object.return_value = {"Body": BytesIO(b"text")}
        bedrock.invoke_model.side_effect = client_error("AccessDeniedException")
        with pytest.raises(ClientError):
            steps.lambda_handler({"action": "extract", "document_id": "doc-1"}, None)

    def test_binary_document_is_terminal(self, s3, bedrock):
        s3.get_object.return_value = {"Body": BytesIO(b"\xff\xfe\x00\x01")}
        with pytest.raises(steps.TerminalError):
            steps.lambda_handler({"action": "extract", "document_id": "doc-1"}, None)
        bedrock.invoke_model.assert_not_called()


class TestValidate:
    def test_high_confidence_completes(self):
        raw = json.dumps({"document_type": "statement", "confidence": 0.95, "fields": {"a": "b"}})
        result = steps.lambda_handler({"action": "validate", "raw_reply": raw}, None)
        assert result["status"] == "COMPLETED"

    def test_low_confidence_needs_review(self):
        raw = json.dumps({"document_type": "other", "confidence": 0.3, "fields": {}})
        result = steps.lambda_handler({"action": "validate", "raw_reply": raw}, None)
        assert result["status"] == "REVIEW"

    def test_unparseable_reply_is_terminal(self):
        with pytest.raises(steps.TerminalError):
            steps.lambda_handler({"action": "validate", "raw_reply": "not json"}, None)


class TestPersistAndFail:
    def test_persist_writes_status_and_usage(self, table):
        steps.lambda_handler(
            {
                "action": "persist",
                "document_id": "doc-1",
                "status": "COMPLETED",
                "result": {"document_type": "statement", "confidence": 0.95, "fields": {}},
                "usage": {"input_tokens": 1, "output_tokens": 2},
            },
            None,
        )
        values = table.update_item.call_args.kwargs["ExpressionAttributeValues"]
        assert values[":status"] == "COMPLETED"
        assert values[":i"] == 1

    def test_fail_marks_document_failed(self, table):
        steps.lambda_handler({"action": "fail", "document_id": "doc-1", "error": "boom"}, None)
        assert table.update_item.call_args.kwargs["ExpressionAttributeValues"][":failed"] == "FAILED"


class TestDispatch:
    def test_unknown_action_raises(self):
        with pytest.raises(ValueError):
            steps.lambda_handler({"action": "nope"}, None)
