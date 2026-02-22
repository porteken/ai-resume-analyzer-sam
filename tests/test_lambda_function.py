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
    def __getattr__(self, name: str) -> Any:
        if name == "Part":
            return _FakePart
        if name == "GenerateContentConfig":
            return _FakeGenerateContentConfig
        raise AttributeError(f"'{type(self).__name__}' has no attribute '{name}'")


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
    lambda_function._get_genai_types = lambda: _FakeTypes()

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
    """Tests for Gemini 2.5 Flash PDF analysis path."""

    def test_analyze_pdf_returns_structured_json(
        self,
        lambda_function_module: Any,
        sample_analyze_event: dict[str, Any],
        mock_lambda_context: Any,
    ) -> None:
        response = lambda_function_module.lambda_handler(sample_analyze_event, mock_lambda_context)

        body = assert_success_response(response, 200, required_fields=["analysis_result", "s3_key"])
        analysis = body["analysis_result"]

        assert analysis["name"] == "Jane Doe"
        assert "skills" in analysis
        assert "experience" in analysis

        call_args = lambda_function_module._get_genai_client().models.generate_content.call_args[1]
        assert call_args["model"] == "gemini-2.5-flash"

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
        self,
        lambda_function_module: Any,
        sample_analyze_event: dict[str, Any],
        mock_lambda_context: Any,
    ) -> None:
        lambda_function_module._get_genai_client().models.generate_content.return_value.text = (
            "not json"
        )

        response = lambda_function_module.lambda_handler(sample_analyze_event, mock_lambda_context)
        body = assert_error_response(response, 500)

        assert "non-JSON" in body["error"]

    def test_analyze_handles_s3_client_error(
        self,
        lambda_function_module: Any,
        sample_analyze_event: dict[str, Any],
        mock_lambda_context: Any,
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

    def test_parse_s3_url_https_without_amazonaws(self, lambda_function_module: Any) -> None:
        with pytest.raises(ValueError, match="valid s3://"):
            lambda_function_module._parse_s3_url("https://example.com/bucket/key.pdf")

    def test_parse_s3_url_https_path_style_invalid_path(self, lambda_function_module: Any) -> None:
        with pytest.raises(ValueError, match="Invalid S3 path-style URL"):
            lambda_function_module._parse_s3_url("https://s3.us-east-1.amazonaws.com/bucket-only")

    def test_parse_s3_url_invalid_format(self, lambda_function_module: Any) -> None:
        with pytest.raises(ValueError, match="valid s3://"):
            lambda_function_module._parse_s3_url("https://example.com/file.pdf")

    def test_download_pdf_empty(
        self, lambda_function_module: Any, mock_boto3_clients: dict[str, Any]
    ) -> None:
        mock_body = MagicMock()
        mock_body.read.return_value = b""
        mock_boto3_clients["s3"].get_object.return_value = {"Body": mock_body}
        with pytest.raises(ValueError, match="Downloaded PDF is empty"):
            lambda_function_module._download_pdf_bytes("bucket", "key.pdf")

    def test_analyze_missing_google_api_key(
        self, lambda_function_module: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(lambda_function_module, "GOOGLE_API_KEY", None)
        with pytest.raises(RuntimeError, match="GOOGLE_API_KEY"):
            lambda_function_module.analyze_resume_pdf(b"%PDF-1.4", "job desc")

    def test_analyze_empty_gemini_response(
        self, lambda_function_module: Any, mock_boto3_clients: dict[str, Any]
    ) -> None:
        mock_client = lambda_function_module._get_genai_client()
        mock_client.models.generate_content.return_value.text = None
        with pytest.raises(RuntimeError, match="empty response"):
            lambda_function_module.analyze_resume_pdf(b"%PDF-1.4", "job desc")

    def test_update_job_status_no_job_id(self, lambda_function_module: Any) -> None:
        lambda_function_module._update_job_status(None, "completed")

    def test_update_job_status_no_table(self, lambda_function_module: Any) -> None:
        original = lambda_function_module.results_table
        lambda_function_module.results_table = None
        try:
            lambda_function_module._update_job_status("job-123", "completed")
        finally:
            lambda_function_module.results_table = original

    def test_update_job_status_exception(
        self, lambda_function_module: Any, mock_boto3_clients: dict[str, Any]
    ) -> None:
        mock_boto3_clients["dynamodb_table"].update_item.side_effect = Exception("DB error")
        lambda_function_module._update_job_status("job-123", "completed")

    def test_get_job_record_no_table(self, lambda_function_module: Any) -> None:
        original = lambda_function_module.results_table
        lambda_function_module.results_table = None
        try:
            result = lambda_function_module._get_job_record("job-123")
            assert result == {}
        finally:
            lambda_function_module.results_table = original

    def test_get_job_record_exception(
        self, lambda_function_module: Any, mock_boto3_clients: dict[str, Any]
    ) -> None:
        mock_boto3_clients["dynamodb_table"].get_item.side_effect = Exception("DB error")
        result = lambda_function_module._get_job_record("job-123")
        assert result == {}

    def test_extract_request_no_body(self, lambda_function_module: Any) -> None:
        body, error = lambda_function_module._extract_request({})
        assert body is None
        assert "No body" in error

    def test_extract_request_base64_encoded(self, lambda_function_module: Any) -> None:
        import base64

        encoded = base64.b64encode(b'{"key": "value"}').decode()
        body, error = lambda_function_module._extract_request(
            {"body": encoded, "isBase64Encoded": True}
        )
        assert error is None
        assert body == {"key": "value"}

    def test_extract_request_invalid_json(self, lambda_function_module: Any) -> None:
        body, error = lambda_function_module._extract_request(
            {"body": "not json", "isBase64Encoded": False}
        )
        assert body is None
        assert "Invalid JSON" in error

    def test_extract_request_body_not_dict(self, lambda_function_module: Any) -> None:
        body, error = lambda_function_module._extract_request(
            {"body": "[]", "isBase64Encoded": False}
        )
        assert body is None
        assert "Invalid JSON body" in error

    def test_job_description_from_record(
        self,
        lambda_function_module: Any,
        mock_lambda_context: Any,
        mock_boto3_clients: dict[str, Any],
    ) -> None:
        mock_boto3_clients["dynamodb_table"].get_item.return_value = {
            "Item": {"job_id": "job-123", "job_description": "From DB"}
        }
        event = {
            "body": '{"job_id": "job-123", "s3_url": "s3://bucket/key.pdf"}',
            "isBase64Encoded": False,
        }
        response = lambda_function_module.lambda_handler(event, mock_lambda_context)
        assert response["statusCode"] == 200

    def test_missing_bucket_key(
        self, lambda_function_module: Any, mock_lambda_context: Any
    ) -> None:
        event = {
            "body": '{"job_id": "job-123", "job_description": "test"}',
            "isBase64Encoded": False,
        }
        response = lambda_function_module.lambda_handler(event, mock_lambda_context)
        assert_error_response(response, 400, "Provide 's3_url'")

    def test_status_update_processing(
        self,
        lambda_function_module: Any,
        sample_analyze_event: dict[str, Any],
        mock_lambda_context: Any,
        mock_boto3_clients: dict[str, Any],
    ) -> None:
        lambda_function_module.lambda_handler(sample_analyze_event, mock_lambda_context)
        calls = mock_boto3_clients["dynamodb_table"].update_item.call_args_list
        statuses = [c[1]["ExpressionAttributeValues"][":status"] for c in calls]
        assert "processing" in statuses
        assert "completed" in statuses

    def test_status_update_failed_on_s3_error(
        self,
        lambda_function_module: Any,
        sample_analyze_event: dict[str, Any],
        mock_lambda_context: Any,
        mock_boto3_clients: dict[str, Any],
    ) -> None:
        from botocore.exceptions import ClientError

        mock_boto3_clients["s3"].get_object.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied"}}, "GetObject"
        )
        response = lambda_function_module.lambda_handler(sample_analyze_event, mock_lambda_context)
        assert_error_response(response, 500)
        calls = mock_boto3_clients["dynamodb_table"].update_item.call_args_list
        statuses = [c[1]["ExpressionAttributeValues"][":status"] for c in calls]
        assert "failed" in statuses

    def test_status_update_failed_on_exception(
        self,
        lambda_function_module: Any,
        sample_analyze_event: dict[str, Any],
        mock_lambda_context: Any,
        mock_boto3_clients: dict[str, Any],
    ) -> None:
        mock_boto3_clients["s3"].get_object.side_effect = Exception("Unexpected error")
        response = lambda_function_module.lambda_handler(sample_analyze_event, mock_lambda_context)
        assert_error_response(response, 500)
        calls = mock_boto3_clients["dynamodb_table"].update_item.call_args_list
        statuses = [c[1]["ExpressionAttributeValues"][":status"] for c in calls]
        assert "failed" in statuses
