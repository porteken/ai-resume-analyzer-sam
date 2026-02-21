"""Unit tests for lambda_function.py."""

import json
from typing import Any
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from tests.test_utils import assert_error_response, assert_success_response


class _FakePart:
    @staticmethod
    def from_bytes(data: bytes, mime_type: str) -> dict[str, Any]:
        return {"data": data, "mime_type": mime_type}


class _FakeGenerateContentConfig:
    def __init__(self, **kwargs: Any) -> None:
        self.__dict__.update(kwargs)


class _FakeTypes:
    Part = _FakePart
    GenerateContentConfig = _FakeGenerateContentConfig


@pytest.fixture
def lambda_function_module(mock_boto3_clients: dict[str, Any]) -> Any:
    """Import lambda_function with mocked dependencies."""
    from resume_analyzer import lambda_function

    original_s3 = lambda_function.s3_client
    original_dynamodb = lambda_function.dynamodb
    original_results_table = lambda_function.results_table
    original_account_id = lambda_function.ACCOUNT_ID
    original_client_factory = lambda_function._get_genai_client
    original_types_factory = lambda_function._get_genai_types

    lambda_function.s3_client = mock_boto3_clients["s3"]
    lambda_function.dynamodb = mock_boto3_clients["dynamodb"]
    lambda_function.results_table = mock_boto3_clients["dynamodb_table"]
    lambda_function.ACCOUNT_ID = "123456789012"

    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.text = json.dumps(
        {
            "name": "Jane Doe",
            "contact_info": {
                "email": "jane@example.com",
                "phone": "555-0100",
                "location": "Austin, TX",
                "linkedin": "https://linkedin.com/in/janedoe",
            },
            "summary": "Senior Python engineer.",
            "skills": ["Python", "AWS"],
            "experience": [
                {
                    "company": "Acme",
                    "role": "Engineer",
                    "duration": "2021-2025",
                    "highlights": ["Built APIs"],
                }
            ],
            "gaps": ["No direct ML production ownership"],
            "recommendations": ["Add ML deployment examples"],
        }
    )
    mock_client.models.generate_content.return_value = mock_response

    lambda_function._get_genai_client = lambda: mock_client
    lambda_function._get_genai_types = lambda: _FakeTypes

    try:
        yield lambda_function
    finally:
        lambda_function.s3_client = original_s3
        lambda_function.dynamodb = original_dynamodb
        lambda_function.results_table = original_results_table
        lambda_function.ACCOUNT_ID = original_account_id
        lambda_function._get_genai_client = original_client_factory
        lambda_function._get_genai_types = original_types_factory


@pytest.mark.unit
class TestGeminiAnalysis:
    """Tests for Gemini 3 Flash PDF analysis path."""

    def test_analyze_pdf_returns_structured_json(
        self, lambda_function_module: Any, sample_analyze_event: dict[str, Any], mock_lambda_context: Any
    ) -> None:
        response = lambda_function_module.lambda_handler(sample_analyze_event, mock_lambda_context)

        body = assert_success_response(response, 200, required_fields=["analysis_result", "s3_key"])
        analysis = body["analysis_result"]

        assert analysis["name"] == "Jane Doe"
        assert "skills" in analysis
        assert "experience" in analysis

        call_args = lambda_function_module._get_genai_client().models.generate_content.call_args[1]
        assert call_args["model"] == "gemini-3-flash-preview"

        config = call_args["config"]
        assert config.response_mime_type == "application/json"
        assert config.response_json_schema["type"] == "object"
        assert "recommendations" in config.response_json_schema["properties"]

    def test_analyze_requires_s3_location(
        self, lambda_function_module: Any, mock_lambda_context: Any
    ) -> None:
        event = {
            "body": json.dumps({"job_id": "test-job-123", "job_description": "Python dev"}),
            "isBase64Encoded": False,
        }

        response = lambda_function_module.lambda_handler(event, mock_lambda_context)
        assert_error_response(response, 400, "Provide 's3_url'")

    def test_analyze_handles_non_json_gemini_output(
        self, lambda_function_module: Any, sample_analyze_event: dict[str, Any], mock_lambda_context: Any
    ) -> None:
        lambda_function_module._get_genai_client().models.generate_content.return_value.text = "not json"

        response = lambda_function_module.lambda_handler(sample_analyze_event, mock_lambda_context)
        body = assert_error_response(response, 500)

        assert "non-JSON" in body["error"]

    def test_analyze_handles_s3_client_error(
        self, lambda_function_module: Any, sample_analyze_event: dict[str, Any], mock_lambda_context: Any
    ) -> None:
        error = ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        lambda_function_module.s3_client.get_object.side_effect = error

        response = lambda_function_module.lambda_handler(sample_analyze_event, mock_lambda_context)
        assert_error_response(response, 500, "S3 access error")

    def test_parse_s3_url_variants(self, lambda_function_module: Any) -> None:
        bucket, key = lambda_function_module._parse_s3_url(
            "https://test-resume-bucket.s3.us-east-1.amazonaws.com/uploads/a/resume.pdf"
        )

        assert bucket == "test-resume-bucket"
        assert key == "uploads/a/resume.pdf"
