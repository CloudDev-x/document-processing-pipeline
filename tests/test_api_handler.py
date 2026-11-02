"""Unit tests for the API handler: tenancy, routes, errors, and serialisation. AWS calls are mocked."""

import json
import os
import uuid
from decimal import Decimal
from unittest.mock import patch

os.environ.setdefault("BUCKET_NAME", "test-bucket")
os.environ.setdefault("TABLE_NAME", "test-table")
os.environ.setdefault("TENANT_BY_KEY_ID", json.dumps({"key-acme": "acme"}))
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

import pytest

from src import api_handler
from src.api_handler import lambda_handler, respond


def event(method, resource, key_id="key-acme", body=None, path_params=None, query=None):
    return {
        "httpMethod": method,
        "resource": resource,
        "body": body,
        "pathParameters": path_params,
        "queryStringParameters": query,
        "requestContext": {"identity": {"apiKeyId": key_id}},
    }


@pytest.fixture
def table():
    with patch.object(api_handler, "table") as mock_table:
        yield mock_table


@pytest.fixture
def presign():
    fake = {"url": "https://example.test/", "fields": {"key": "x"}}
    with patch.object(api_handler.s3, "generate_presigned_post", return_value=fake) as mock:
        yield mock


class TestTenancy:
    def test_unknown_key_is_forbidden(self, table):
        resp = lambda_handler(event("GET", "/documents", key_id="key-unknown"), None)
        assert resp["statusCode"] == 403
        table.query.assert_not_called()

    def test_missing_identity_is_forbidden(self, table):
        resp = lambda_handler({"httpMethod": "GET", "resource": "/documents"}, None)
        assert resp["statusCode"] == 403


class TestCreateUpload:
    def test_returns_presigned_post_under_tenant_prefix(self, presign):
        resp = lambda_handler(event("POST", "/documents", body='{"content_type":"text/plain"}'), None)
        assert resp["statusCode"] == 201
        body = json.loads(resp["body"])
        uuid.UUID(body["document_id"])
        assert presign.call_args.kwargs["Key"] == f"acme/{body['document_id']}"
        assert presign.call_args.kwargs["ExpiresIn"] == 300
        assert ["content-length-range", 1, api_handler.MAX_UPLOAD_BYTES] in presign.call_args.kwargs["Conditions"]

    def test_defaults_to_text_plain_without_body(self, presign):
        resp = lambda_handler(event("POST", "/documents", body=None), None)
        assert resp["statusCode"] == 201
        assert presign.call_args.kwargs["Fields"]["Content-Type"] == "text/plain"

    def test_accepts_pdf(self, presign):
        resp = lambda_handler(event("POST", "/documents", body='{"content_type":"application/pdf"}'), None)
        assert resp["statusCode"] == 201

    def test_rejects_unsupported_content_type(self, presign):
        resp = lambda_handler(event("POST", "/documents", body='{"content_type":"application/zip"}'), None)
        assert resp["statusCode"] == 400
        presign.assert_not_called()

    def test_rejects_invalid_json(self, presign):
        resp = lambda_handler(event("POST", "/documents", body="not json"), None)
        assert resp["statusCode"] == 400
        presign.assert_not_called()


class TestGetDocument:
    def test_returns_parsed_extraction_and_int_attempts(self, table):
        table.get_item.return_value = {
            "Item": {
                "tenant_id": "acme",
                "document_id": "abc",
                "status": "COMPLETED",
                "attempts": Decimal("1"),
                "extraction": json.dumps({"document_type": "statement", "confidence": 0.95, "fields": {}}),
            }
        }
        resp = lambda_handler(event("GET", "/documents/{id}", path_params={"id": "abc"}), None)
        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert body["attempts"] == 1
        assert body["extraction"]["document_type"] == "statement"
        table.get_item.assert_called_once_with(Key={"tenant_id": "acme", "document_id": "abc"})

    def test_not_found(self, table):
        table.get_item.return_value = {}
        resp = lambda_handler(event("GET", "/documents/{id}", path_params={"id": "missing"}), None)
        assert resp["statusCode"] == 404


class TestListDocuments:
    def test_queries_only_the_callers_tenant(self, table):
        table.query.return_value = {"Items": [{"document_id": "a"}, {"document_id": "b"}]}
        resp = lambda_handler(event("GET", "/documents"), None)
        assert json.loads(resp["body"])["count"] == 2
        kwargs = table.query.call_args.kwargs
        assert kwargs["KeyConditionExpression"] == "tenant_id = :t"
        assert kwargs["ExpressionAttributeValues"] == {":t": "acme"}
        assert "FilterExpression" not in kwargs

    def test_filters_by_status(self, table):
        table.query.return_value = {"Items": []}
        resp = lambda_handler(event("GET", "/documents", query={"status": "REVIEW"}), None)
        assert resp["statusCode"] == 200
        kwargs = table.query.call_args.kwargs
        assert kwargs["FilterExpression"] == "#s = :status"
        assert kwargs["ExpressionAttributeValues"] == {":t": "acme", ":status": "REVIEW"}


class TestRouting:
    def test_unknown_route_returns_405(self):
        resp = lambda_handler(event("DELETE", "/documents"), None)
        assert resp["statusCode"] == 405


class TestSerialisation:
    def test_decimals_become_int_or_float(self):
        body = json.loads(respond(200, {"whole": Decimal("2"), "fraction": Decimal("0.5")})["body"])
        assert body == {"whole": 2, "fraction": 0.5}
