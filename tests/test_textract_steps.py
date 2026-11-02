"""Unit tests for the classify, Textract, and text-extraction steps. AWS calls are mocked."""

import json
import os
from io import BytesIO
from unittest.mock import MagicMock, patch

os.environ.setdefault("BUCKET_NAME", "test-bucket")
os.environ.setdefault("TABLE_NAME", "test-table")
os.environ.setdefault("MODEL_ID", "test-model")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

import pytest

from src import steps


def bedrock_reply(text):
    body = MagicMock()
    body.read.return_value = json.dumps(
        {"content": [{"text": text}], "usage": {"input_tokens": 1, "output_tokens": 1}}
    ).encode()
    return {"body": body}


@pytest.fixture
def s3():
    with patch.object(steps, "s3") as mock:
        yield mock


@pytest.fixture
def table():
    with patch.object(steps, "table") as mock:
        yield mock


@pytest.fixture
def bedrock():
    with patch.object(steps, "bedrock") as mock:
        yield mock


class TestClassify:
    @pytest.mark.parametrize(
        "content_type, route",
        [("text/plain", "text"), ("application/pdf", "textract"), ("image/png", "textract")],
    )
    def test_routes_by_content_type(self, s3, content_type, route):
        s3.head_object.return_value = {"ContentType": content_type}
        result = steps.lambda_handler({"action": "classify", "document_id": "doc-1"}, None)
        assert result["route"] == route

    def test_ignores_charset_parameter(self, s3):
        s3.head_object.return_value = {"ContentType": "text/plain; charset=utf-8"}
        result = steps.lambda_handler({"action": "classify", "document_id": "doc-1"}, None)
        assert result["route"] == "text"

    def test_unknown_type_is_terminal(self, s3):
        s3.head_object.return_value = {"ContentType": "application/octet-stream"}
        with pytest.raises(steps.TerminalError):
            steps.lambda_handler({"action": "classify", "document_id": "doc-1"}, None)


class TestStartTextract:
    def test_saves_token_then_starts_job_with_tag_and_notification(self, table):
        with patch.object(steps, "textract") as tx, patch.object(steps, "TEXTRACT_TOPIC_ARN", "arn:topic"), patch.object(
            steps, "TEXTRACT_ROLE_ARN", "arn:role"
        ):
            tx.start_document_text_detection.return_value = {"JobId": "job-1"}
            result = steps.lambda_handler(
                {"action": "start_textract", "document_id": "doc-1", "task_token": "tok-1"}, None
            )
        assert result == {"job_id": "job-1"}
        assert table.update_item.call_args_list[0].kwargs["ExpressionAttributeValues"][":t"] == "tok-1"
        kwargs = tx.start_document_text_detection.call_args.kwargs
        assert kwargs["JobTag"] == "doc-1"
        assert kwargs["NotificationChannel"] == {"SNSTopicArn": "arn:topic", "RoleArn": "arn:role"}

    def test_unsafe_document_id_is_rejected(self, table):
        with patch.object(steps, "textract") as tx:
            with pytest.raises(steps.TerminalError):
                steps.lambda_handler(
                    {"action": "start_textract", "document_id": "bad key with spaces", "task_token": "t"}, None
                )
            tx.start_document_text_detection.assert_not_called()


class TestReadTextract:
    def test_stores_text_in_scratch_bucket(self, table, s3):
        table.get_item.return_value = {"Item": {"document_id": "doc-1", "textract_job_id": "job-1"}}
        with patch.object(steps, "get_text_detection", return_value=("SUCCEEDED", "Opening balance 100")), patch.object(
            steps, "SCRATCH_BUCKET_NAME", "scratch-bucket"
        ):
            result = steps.lambda_handler({"action": "read_textract", "document_id": "doc-1"}, None)
        assert result == {"text_key": "text/doc-1.txt"}
        s3.put_object.assert_called_once()
        assert s3.put_object.call_args.kwargs["Bucket"] == "scratch-bucket"

    def test_failed_job_is_terminal(self, table, s3):
        table.get_item.return_value = {"Item": {"document_id": "doc-1", "textract_job_id": "job-1"}}
        with patch.object(steps, "get_text_detection", return_value=("FAILED", "unsupported")):
            with pytest.raises(steps.TerminalError):
                steps.lambda_handler({"action": "read_textract", "document_id": "doc-1"}, None)

    def test_running_job_is_transient(self, table, s3):
        table.get_item.return_value = {"Item": {"document_id": "doc-1", "textract_job_id": "job-1"}}
        with patch.object(steps, "get_text_detection", return_value=("IN_PROGRESS", None)):
            with pytest.raises(steps.TransientError):
                steps.lambda_handler({"action": "read_textract", "document_id": "doc-1"}, None)


class TestExtractFromScratch:
    def test_reads_scratch_text_when_text_key_given(self, s3, bedrock):
        s3.get_object.return_value = {"Body": BytesIO(b"Opening balance 100")}
        bedrock.invoke_model.return_value = bedrock_reply('{"document_type":"statement"}')
        with patch.object(steps, "SCRATCH_BUCKET_NAME", "scratch-bucket"):
            steps.lambda_handler(
                {"action": "extract", "document_id": "doc-1", "text_key": "text/doc-1.txt"}, None
            )
        s3.get_object.assert_called_once_with(Bucket="scratch-bucket", Key="text/doc-1.txt")
