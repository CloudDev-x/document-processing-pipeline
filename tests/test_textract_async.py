"""Unit tests for the async Textract helper. Textract calls are mocked."""

import os
from unittest.mock import MagicMock, patch

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

import pytest

from src import textract_async


@pytest.fixture
def client():
    with patch.object(textract_async, "textract") as mock:
        yield mock


def test_start_passes_s3_location(client):
    client.start_document_text_detection.return_value = {"JobId": "job-1"}
    assert textract_async.start_text_detection("bkt", "doc.pdf") == "job-1"
    client.start_document_text_detection.assert_called_once_with(
        DocumentLocation={"S3Object": {"Bucket": "bkt", "Name": "doc.pdf"}}
    )


def test_get_joins_lines_across_pages(client):
    client.get_document_text_detection.side_effect = [
        {"JobStatus": "SUCCEEDED", "Blocks": [{"BlockType": "LINE", "Text": "page one"}], "NextToken": "t1"},
        {
            "JobStatus": "SUCCEEDED",
            "Blocks": [
                {"BlockType": "WORD", "Text": "ignored"},
                {"BlockType": "LINE", "Text": "page two"},
            ],
        },
    ]
    status, text = textract_async.get_text_detection("job-1")
    assert status == "SUCCEEDED"
    assert text == "page one\npage two"
    assert client.get_document_text_detection.call_args_list[1].kwargs["NextToken"] == "t1"


def test_in_progress_returns_no_text(client):
    client.get_document_text_detection.return_value = {"JobStatus": "IN_PROGRESS"}
    assert textract_async.get_text_detection("job-1") == ("IN_PROGRESS", None)


def test_failed_returns_status_message(client):
    client.get_document_text_detection.return_value = {
        "JobStatus": "FAILED",
        "StatusMessage": "unsupported document",
    }
    assert textract_async.get_text_detection("job-1") == ("FAILED", "unsupported document")


def test_wait_times_out(client):
    client.get_document_text_detection.return_value = {"JobStatus": "IN_PROGRESS"}
    with patch.object(textract_async, "time") as fake_time:
        fake_time.monotonic.side_effect = [0, 1000]
        with pytest.raises(TimeoutError):
            textract_async.wait_for_text("job-1", timeout_seconds=300, poll_seconds=5)
