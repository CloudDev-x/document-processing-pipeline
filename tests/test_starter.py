"""Unit tests for the starter Lambda that claims documents and starts workflows. AWS calls are mocked."""

import json
import os
from unittest.mock import patch

os.environ.setdefault("TABLE_NAME", "test-table")
os.environ.setdefault("STATE_MACHINE_ARN", "arn:aws:states:us-east-1:123456789012:stateMachine:test")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

import pytest
from botocore.exceptions import ClientError

from src import handler as starter


def s3_event(key, receive_count=1):
    body = {"Records": [{"s3": {"object": {"key": key}}}]}
    return {
        "Records": [
            {"attributes": {"ApproximateReceiveCount": str(receive_count)}, "body": json.dumps(body)}
        ]
    }


@pytest.fixture
def sfn():
    with patch.object(starter, "sfn") as mock:
        yield mock


@pytest.fixture
def claim():
    with patch.object(starter, "claim", return_value=True) as mock:
        yield mock


def test_execution_name_is_sanitised():
    assert starter.execution_name("a b/c.txt", 1) == "a_b_c_txt-1"


def test_execution_name_is_at_most_80_chars():
    assert len(starter.execution_name("x" * 200, 1)) <= 80


def test_starts_one_workflow_per_document(sfn, claim):
    starter.lambda_handler(s3_event("doc-1"), None)
    kwargs = sfn.start_execution.call_args.kwargs
    assert kwargs["name"] == "doc-1-1"
    assert json.loads(kwargs["input"]) == {"document_id": "doc-1"}


def test_duplicate_claim_starts_nothing(sfn, claim):
    claim.return_value = False
    starter.lambda_handler(s3_event("doc-1"), None)
    sfn.start_execution.assert_not_called()


def test_already_started_is_not_an_error(sfn, claim):
    sfn.start_execution.side_effect = ClientError(
        {"Error": {"Code": "ExecutionAlreadyExists", "Message": "x"}}, "StartExecution"
    )
    starter.lambda_handler(s3_event("doc-1"), None)


def test_test_event_is_ignored(sfn, claim):
    event = {"Records": [{"attributes": {"ApproximateReceiveCount": "1"}, "body": json.dumps({"Event": "s3:TestEvent"})}]}
    starter.lambda_handler(event, None)
    claim.assert_not_called()
    sfn.start_execution.assert_not_called()
