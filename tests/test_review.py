"""Unit tests for the review endpoint. AWS calls are mocked."""

import json
from unittest.mock import patch

import pytest
from botocore.exceptions import ClientError

from src import api_handler
from src.api_handler import lambda_handler


def review_event(document_id, body, key_id="key-acme"):
    return {
        "httpMethod": "POST",
        "resource": "/documents/{id}/review",
        "body": json.dumps(body) if body is not None else None,
        "pathParameters": {"id": document_id},
        "queryStringParameters": None,
        "requestContext": {"identity": {"apiKeyId": key_id}},
    }


@pytest.fixture
def table():
    with patch.object(api_handler, "table") as mock_table:
        yield mock_table


def in_review():
    return {"Item": {"tenant_id": "acme", "document_id": "doc-1", "status": "REVIEW"}}


def test_approve_moves_review_to_completed(table):
    table.get_item.return_value = in_review()
    resp = lambda_handler(review_event("doc-1", {"decision": "approve"}), None)
    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["status"] == "COMPLETED"
    kwargs = table.update_item.call_args.kwargs
    assert kwargs["Key"] == {"tenant_id": "acme", "document_id": "doc-1"}
    assert kwargs["ConditionExpression"] == "#s = :review"
    assert kwargs["ExpressionAttributeValues"][":new"] == "COMPLETED"
    assert kwargs["ExpressionAttributeValues"][":by"] == "key-acme"


def test_reject_moves_review_to_failed_with_reason(table):
    table.get_item.return_value = in_review()
    resp = lambda_handler(review_event("doc-1", {"decision": "reject"}), None)
    assert json.loads(resp["body"])["status"] == "FAILED"
    values = table.update_item.call_args.kwargs["ExpressionAttributeValues"]
    assert values[":new"] == "FAILED"
    assert values[":err"] == "rejected in review"


def test_corrections_are_stored(table):
    table.get_item.return_value = in_review()
    corrections = {"balance": "NGN 312,450.75"}
    lambda_handler(review_event("doc-1", {"decision": "approve", "corrections": corrections}), None)
    assert table.update_item.call_args.kwargs["ExpressionAttributeValues"][":corrections"] == corrections


def test_document_not_in_review_is_conflict(table):
    table.get_item.return_value = {"Item": {"status": "COMPLETED"}}
    resp = lambda_handler(review_event("doc-1", {"decision": "approve"}), None)
    assert resp["statusCode"] == 409
    table.update_item.assert_not_called()


def test_missing_document_is_not_found(table):
    table.get_item.return_value = {}
    resp = lambda_handler(review_event("missing", {"decision": "approve"}), None)
    assert resp["statusCode"] == 404


def test_invalid_decision_is_rejected(table):
    resp = lambda_handler(review_event("doc-1", {"decision": "maybe"}), None)
    assert resp["statusCode"] == 400
    table.get_item.assert_not_called()


def test_invalid_corrections_are_rejected(table):
    resp = lambda_handler(review_event("doc-1", {"decision": "approve", "corrections": ["x"]}), None)
    assert resp["statusCode"] == 400


def test_lost_race_is_conflict(table):
    table.get_item.return_value = in_review()
    table.update_item.side_effect = ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": "changed"}}, "UpdateItem"
    )
    resp = lambda_handler(review_event("doc-1", {"decision": "approve"}), None)
    assert resp["statusCode"] == 409
