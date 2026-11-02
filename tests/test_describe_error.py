"""Unit tests for turning a Step Functions error into a readable last_error."""

import json
import os

os.environ.setdefault("BUCKET_NAME", "test-bucket")
os.environ.setdefault("TABLE_NAME", "test-table")
os.environ.setdefault("MODEL_ID", "test-model")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

from src import steps


def test_lambda_error_shows_the_message():
    raw = json.dumps(
        {
            "Error": "TerminalError",
            "Cause": json.dumps({"errorMessage": "unsupported content type: application/octet-stream"}),
        }
    )
    assert steps.describe_error(raw) == "TerminalError: unsupported content type: application/octet-stream"


def test_plain_text_passes_through():
    assert steps.describe_error("boom") == "boom"
