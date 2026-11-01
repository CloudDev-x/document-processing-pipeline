"""Unit tests for handler validation and parsing logic."""

import os
import json

# Set required env vars before importing handler
os.environ.setdefault("TABLE_NAME", "test-table")
os.environ.setdefault("BUCKET_NAME", "test-bucket")
os.environ.setdefault("MODEL_ID", "test-model")

import pytest
from unittest.mock import patch, MagicMock

# Patch boto3 clients before importing handler
with patch("boto3.resource"), patch("boto3.client"):
    from src.handler import parse_model_json, validate_result, ALLOWED_TYPES, REVIEW_THRESHOLD


class TestParseModelJson:
    def test_bare_json(self):
        raw = '{"document_type": "statement", "confidence": 0.9, "fields": {}}'
        assert parse_model_json(raw)["document_type"] == "statement"

    def test_json_with_preamble(self):
        raw = 'Here is the extraction:\n{"document_type": "id", "confidence": 0.85, "fields": {"name": "Alice"}}'
        result = parse_model_json(raw)
        assert result["document_type"] == "id"
        assert result["fields"]["name"] == "Alice"

    def test_json_with_trailing_text(self):
        raw = '{"document_type": "loan_form", "confidence": 0.7, "fields": {}}\nLet me know.'
        assert parse_model_json(raw)["document_type"] == "loan_form"

    def test_no_json_raises(self):
        with pytest.raises(ValueError):
            parse_model_json("No JSON here at all")

    def test_empty_string_raises(self):
        with pytest.raises(ValueError):
            parse_model_json("")


class TestValidateResult:
    def test_valid_result(self):
        r = {"document_type": "statement", "confidence": 0.95, "fields": {"ref": "123"}}
        validate_result(r)

    def test_missing_document_type(self):
        with pytest.raises(ValueError, match="document_type"):
            validate_result({"confidence": 0.9, "fields": {}})

    def test_invalid_document_type(self):
        with pytest.raises(ValueError, match="document_type"):
            validate_result({"document_type": "invoice", "confidence": 0.9, "fields": {}})

    def test_confidence_not_number(self):
        with pytest.raises(ValueError, match="confidence"):
            validate_result({"document_type": "statement", "confidence": "high", "fields": {}})

    def test_confidence_out_of_range(self):
        with pytest.raises(ValueError, match="confidence"):
            validate_result({"document_type": "statement", "confidence": 1.5, "fields": {}})

    def test_confidence_boolean_rejected(self):
        with pytest.raises(ValueError, match="confidence"):
            validate_result({"document_type": "statement", "confidence": True, "fields": {}})

    def test_missing_fields(self):
        with pytest.raises(ValueError, match="fields"):
            validate_result({"document_type": "statement", "confidence": 0.9})

    def test_fields_not_dict(self):
        with pytest.raises(ValueError, match="fields"):
            validate_result({"document_type": "statement", "confidence": 0.9, "fields": "none"})

    def test_all_allowed_types(self):
        for t in ALLOWED_TYPES:
            validate_result({"document_type": t, "confidence": 0.5, "fields": {}})


class TestReviewThreshold:
    def test_low_confidence_triggers_review(self):
        assert 0.3 < REVIEW_THRESHOLD
        assert 0.95 >= REVIEW_THRESHOLD
