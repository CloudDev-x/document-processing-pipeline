"""Unit tests for the Textract completion callback. AWS calls are mocked."""

import json
import os
from unittest.mock import patch

os.environ.setdefault("TABLE_NAME", "test-table")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

import pytest
from botocore.exceptions import ClientError

from src import textract_callback


@pytest.fixture
def table():
    with patch.object(textract_callback, "table") as mock_table:
        yield mock_table


@pytest.fixture
def sfn():
    with patch.object(textract_callback, "sfn") as mock_sfn:
        yield mock_sfn


def sns_event(message):
    return {"Records": [{"Sns": {"Message": json.dumps(message)}}]}


def pending(token="tok-1"):
    return {"Item": {"document_id": "doc-1", "task_token": token}}


SUCCEEDED = {"JobId": "job-1", "Status": "SUCCEEDED", "JobTag": "doc-1"}


def test_succeeded_resumes_workflow(table, sfn):
    table.get_item.return_value = pending()
    result = textract_callback.lambda_handler(sns_event(SUCCEEDED), None)
    assert result == {"results": ["resumed"]}
    kwargs = sfn.send_task_success.call_args.kwargs
    assert kwargs["taskToken"] == "tok-1"
    assert json.loads(kwargs["output"]) == {"document_id": "doc-1", "textract_job_id": "job-1"}


def test_claim_is_conditional_on_the_token(table, sfn):
    table.get_item.return_value = pending()
    textract_callback.lambda_handler(sns_event(SUCCEEDED), None)
    kwargs = table.update_item.call_args.kwargs
    assert kwargs["UpdateExpression"] == "REMOVE task_token"
    assert kwargs["ExpressionAttributeValues"] == {":token": "tok-1"}


def test_failed_status_fails_workflow(table, sfn):
    table.get_item.return_value = pending()
    message = {"JobId": "job-1", "Status": "FAILED", "JobTag": "doc-1"}
    result = textract_callback.lambda_handler(sns_event(message), None)
    assert result == {"results": ["failed"]}
    assert sfn.send_task_failure.call_args.kwargs["taskToken"] == "tok-1"
    sfn.send_task_success.assert_not_called()


def test_missing_job_tag_is_ignored(table, sfn):
    message = {"JobId": "job-1", "Status": "SUCCEEDED"}
    result = textract_callback.lambda_handler(sns_event(message), None)
    assert result == {"results": ["ignored: no JobTag"]}
    table.get_item.assert_not_called()
    sfn.send_task_success.assert_not_called()


def test_no_pending_task_is_ignored(table, sfn):
    table.get_item.return_value = {"Item": {"document_id": "doc-1"}}
    result = textract_callback.lambda_handler(sns_event(SUCCEEDED), None)
    assert result == {"results": ["ignored: no pending task"]}
    table.update_item.assert_not_called()
    sfn.send_task_success.assert_not_called()


def test_duplicate_delivery_does_not_resume_twice(table, sfn):
    table.get_item.return_value = pending()
    table.update_item.side_effect = ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": "claimed"}},
        "UpdateItem",
    )
    result = textract_callback.lambda_handler(sns_event(SUCCEEDED), None)
    assert result == {"results": ["ignored: already claimed"]}
    sfn.send_task_success.assert_not_called()
