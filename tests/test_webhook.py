"""Unit tests for webhook delivery: signing, skipping, and retry on failure. HTTP and SSM are mocked."""

import hashlib
import hmac
import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from src import webhook_delivery as wd

MESSAGE = {
    "tenant_id": "acme",
    "document_id": "doc-1",
    "status": "COMPLETED",
    "updated_at": "2026-11-02T10:00:00+00:00",
}


def sqs_event(message):
    return {"Records": [{"body": json.dumps({"Type": "Notification", "Message": json.dumps(message)})}]}


def ok_response():
    resp = MagicMock()
    resp.status = 200
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


def test_sign_matches_hmac_sha256():
    expected = "sha256=" + hmac.new(b"s3cret", b"123.body", hashlib.sha256).hexdigest()
    assert wd.sign("s3cret", "123", "body") == expected


def test_skipped_when_not_configured():
    with patch.object(wd, "tenant_settings", return_value=("disabled", "disabled")), patch.object(
        wd.urllib.request, "urlopen"
    ) as urlopen:
        result = wd.lambda_handler(sqs_event(MESSAGE), None)
    assert result == {"results": ["skipped: webhook not configured"]}
    urlopen.assert_not_called()


def test_refuses_non_https_urls():
    with patch.object(wd, "tenant_settings", return_value=("http://insecure.test/hook", "s3cret")), patch.object(
        wd.urllib.request, "urlopen"
    ) as urlopen:
        result = wd.lambda_handler(sqs_event(MESSAGE), None)
    assert result == {"results": ["skipped: webhook URL is not HTTPS"]}
    urlopen.assert_not_called()


def test_delivers_signed_post():
    with patch.object(wd, "tenant_settings", return_value=("https://example.test/hook", "s3cret")), patch.object(
        wd.urllib.request, "urlopen", return_value=ok_response()
    ) as urlopen:
        result = wd.lambda_handler(sqs_event(MESSAGE), None)
    assert result == {"results": ["delivered"]}

    request = urlopen.call_args.args[0]
    body = request.data.decode("utf-8")
    payload = json.loads(body)
    assert payload["event_id"] == "doc-1:COMPLETED:2026-11-02T10:00:00+00:00"
    assert payload["tenant_id"] == "acme"

    headers = {k.lower(): v for k, v in request.headers.items()}
    assert headers["x-docpipe-event-id"] == payload["event_id"]
    assert headers["x-docpipe-signature"] == wd.sign("s3cret", headers["x-docpipe-timestamp"], body)


def test_server_error_raises_so_sqs_retries():
    error = urllib.error.HTTPError("https://example.test/hook", 500, "boom", {}, None)
    with patch.object(wd, "tenant_settings", return_value=("https://example.test/hook", "s3cret")), patch.object(
        wd.urllib.request, "urlopen", side_effect=error
    ):
        with pytest.raises(urllib.error.HTTPError):
            wd.lambda_handler(sqs_event(MESSAGE), None)
