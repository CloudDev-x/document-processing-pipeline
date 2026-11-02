"""Shared test setup: environment defaults and cached model settings, so no test calls SSM."""

import os

os.environ.setdefault("BUCKET_NAME", "test-bucket")
os.environ.setdefault("TABLE_NAME", "test-table")
os.environ.setdefault("MODEL_ID", "test-model")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("TENANT_BY_KEY_ID", '{"key-acme": "acme"}')
os.environ.setdefault("STATE_MACHINE_ARN", "arn:aws:states:us-east-1:123456789012:stateMachine:test")
os.environ.setdefault("AUDIT_TABLE_NAME", "test-audit")
os.environ.setdefault("EVENTS_TOPIC_ARN", "arn:aws:sns:us-east-1:123456789012:test")

from src import steps  # noqa: E402

steps._config = {"model_id": "test-model", "review_threshold": 0.8, "extraction_prompt": "test prompt"}
