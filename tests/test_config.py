"""Unit tests for reading model settings from SSM. AWS calls are mocked."""

from unittest.mock import patch

from src import steps


def test_reads_all_settings_once(monkeypatch):
    monkeypatch.setattr(steps, "_config", None)
    fake = {
        "Parameters": [
            {"Name": "/docpipe/model_id", "Value": "model-x"},
            {"Name": "/docpipe/review_threshold", "Value": "0.7"},
            {"Name": "/docpipe/extraction_prompt", "Value": "prompt-x"},
        ]
    }
    with patch.object(steps, "ssm") as ssm:
        ssm.get_parameters.return_value = fake
        first = steps.get_config()
        second = steps.get_config()
    assert first == {"model_id": "model-x", "review_threshold": 0.7, "extraction_prompt": "prompt-x"}
    assert second is first
    assert ssm.get_parameters.call_count == 1
