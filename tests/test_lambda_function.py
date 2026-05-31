"""Unit tests for lambda_function.py."""

import json
from typing import Any
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from tests.conftest import FakeTypes
from tests.test_utils import assert_error_response, assert_success_response


@pytest.fixture
def lambda_function_module(mock_boto3_clients: dict[str, Any]) -> Any:
    """Import lambda_function with mocked dependencies."""
    from resume_analyzer import lambda_function

    original_account_id = lambda_function.ACCOUNT_ID
    original_get_results_table = lambda_function.get_results_table
    original_get_s3_client = lambda_function.get_s3_client
    original_get_lambda_client = lambda_function.get_lambda_client
    original_get_secrets_client = lambda_function._get_secrets_client
    original_client_class = lambda_function.Client
    original_types = lambda_function.types
    original_genai_client = lambda_function._genai_client
    original_genai_client_api_key = lambda_function._genai_client_api_key
    original_secrets_client = lambda_function._secrets_client
    original_cached_secret_arn = lambda_function._cached_secret_arn
    original_cached_google_api_key = lambda_function._cached_google_api_key

    mock_boto3_clients["dynamodb_table"].get_item.return_value = {
        "Item": {
            "job_id": "test-job-123",
            "status": "upload_pending",
            "s3_bucket": "test-resume-bucket",
            "s3_key": "uploads/test-job-123/test-resume.pdf",
            "job_description": "Python developer position",
        }
    }

    lambda_function.get_s3_client = lambda: mock_boto3_clients["s3"]
    lambda_function.get_lambda_client = lambda: mock_boto3_clients["lambda"]
    lambda_function.ACCOUNT_ID = "123456789012"
    lambda_function.get_results_table = lambda: mock_boto3_clients["dynamodb_table"]
    mock_secrets_client = MagicMock()
    mock_secrets_client.get_secret_value.return_value = {
        "SecretString": json.dumps({"GOOGLE_API_KEY": "test-api-key-123"})
    }
    lambda_function._get_secrets_client = lambda: mock_secrets_client

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
            "education": [],
            "strengths": ["Strong Python backend development"],
            "gaps": ["No direct ML production ownership"],
            "recommendations": ["Add ML deployment examples"],
        }
    )
    mock_client.models.generate_content.return_value = mock_response

    lambda_function.Client = MagicMock(return_value=mock_client)
    lambda_function.types = FakeTypes()
    lambda_function._genai_client = None
    lambda_function._genai_client_api_key = None
    lambda_function._secrets_client = None
    lambda_function._cached_secret_arn = None
    lambda_function._cached_google_api_key = None

    try:
        yield lambda_function
    finally:
        lambda_function.ACCOUNT_ID = original_account_id
        lambda_function.get_results_table = original_get_results_table
        lambda_function.get_s3_client = original_get_s3_client
        lambda_function.get_lambda_client = original_get_lambda_client
        lambda_function._get_secrets_client = original_get_secrets_client
        lambda_function.Client = original_client_class
        lambda_function.types = original_types
        lambda_function._genai_client = original_genai_client
        lambda_function._genai_client_api_key = original_genai_client_api_key
        lambda_function._secrets_client = original_secrets_client
        lambda_function._cached_secret_arn = original_cached_secret_arn
        lambda_function._cached_google_api_key = original_cached_google_api_key


class FakeServerUnavailableError(Exception):
    def __init__(self, message: str = "503 high demand") -> None:
        super().__init__(message)
        self.code = 503


class FakeRateLimitError(Exception):
    def __init__(
        self,
        message: str = "429 rate limit exceeded",
        *,
        status_code: int | None = 429,
        code: int | str | None = None,
    ) -> None:
        super().__init__(message)
        if status_code is not None:
            self.status_code = status_code
        if code is not None:
            self.code = code


@pytest.mark.unit
class TestGeminiAnalysis:
    """Tests for Gemini 3 Flash Preview PDF analysis path."""

    def test_analyze_pdf_returns_structured_json(
        self,
        lambda_function_module: Any,
        mock_lambda_context: Any,
        mock_boto3_clients: dict[str, Any],
    ) -> None:
        mock_boto3_clients["dynamodb_table"].get_item.return_value = {
            "Item": {
                "job_id": "test-job-123",
                "status": "queued",
                "s3_bucket": "test-resume-bucket",
                "s3_key": "uploads/test-job-123/test-resume.pdf",
                "job_description": "Python developer position",
            }
        }
        response = lambda_function_module.lambda_handler(
            {"source": lambda_function_module.INTERNAL_WORKER_SOURCE, "job_id": "test-job-123"},
            mock_lambda_context,
        )

        assert response["status"] == "completed"
        completed_call = mock_boto3_clients["dynamodb_table"].update_item.call_args_list[-1]
        analysis = completed_call[1]["ExpressionAttributeValues"][":result"]

        assert analysis["name"] == "Jane Doe"
        assert "skills" in analysis
        assert "experience" in analysis
        assert "education" in analysis
        assert "strengths" in analysis
        assert "gaps" in analysis
        assert "recommendations" in analysis

        call_args = lambda_function_module._get_genai_client().models.generate_content.call_args[1]
        assert call_args["model"] == "gemini-3-flash-preview"

        config = call_args["config"]
        assert config.response_mime_type == "application/json"
        assert config.response_json_schema["type"] == "object"
        assert "education" in config.response_json_schema["properties"]
        assert "strengths" in config.response_json_schema["properties"]
        assert "recommendations" in config.response_json_schema["properties"]

        prompt = call_args["contents"][1]
        assert "Job Description" in prompt
        assert "education" in prompt.lower()
        assert "Python developer position" in prompt

    def test_analyze_backfills_missing_strengths_gaps_and_recommendations(
        self,
        lambda_function_module: Any,
        mock_lambda_context: Any,
        mock_boto3_clients: dict[str, Any],
    ) -> None:
        mock_boto3_clients["dynamodb_table"].get_item.return_value = {
            "Item": {
                "job_id": "test-job-123",
                "status": "queued",
                "s3_bucket": "test-resume-bucket",
                "s3_key": "uploads/test-job-123/test-resume.pdf",
                "job_description": "Python developer position",
            }
        }
        lambda_function_module._get_genai_client().models.generate_content.return_value.text = (
            json.dumps(
                {
                    "name": "Jane Doe",
                    "contact_info": {
                        "email": "",
                        "phone": "",
                        "location": "",
                        "linkedin": "",
                    },
                    "summary": "",
                    "skills": [],
                    "experience": [],
                }
            )
        )

        response = lambda_function_module.lambda_handler(
            {"source": lambda_function_module.INTERNAL_WORKER_SOURCE, "job_id": "test-job-123"},
            mock_lambda_context,
        )
        assert response["status"] == "completed"
        completed_call = mock_boto3_clients["dynamodb_table"].update_item.call_args_list[-1]
        analysis = completed_call[1]["ExpressionAttributeValues"][":result"]

        assert analysis["education"] == []
        assert analysis["strengths"] == []
        assert analysis["gaps"] == []
        assert analysis["recommendations"] == []

    def test_analyze_requires_s3_location(
        self, lambda_function_module: Any, mock_lambda_context: Any
    ) -> None:
        event = {"body": json.dumps({"job_description": "Python dev"}), "isBase64Encoded": False}

        response = lambda_function_module.lambda_handler(event, mock_lambda_context)
        assert_error_response(response, 400, "job_id is required")

    def test_queue_analyze_returns_accepted_and_invokes_worker(
        self,
        lambda_function_module: Any,
        sample_analyze_event: dict[str, Any],
        mock_lambda_context: Any,
        mock_boto3_clients: dict[str, Any],
    ) -> None:
        response = lambda_function_module.lambda_handler(sample_analyze_event, mock_lambda_context)

        body = assert_success_response(response, 202, required_fields=["job_id", "status_url"])
        assert body["status"] == "queued"
        assert "analysis_result" not in body
        mock_boto3_clients["lambda"].invoke.assert_called_once()
        invoke_args = mock_boto3_clients["lambda"].invoke.call_args[1]
        assert invoke_args["InvocationType"] == "Event"

    def test_duplicate_analyze_conditional_race_does_not_invoke_worker(
        self,
        lambda_function_module: Any,
        sample_analyze_event: dict[str, Any],
        mock_lambda_context: Any,
        mock_boto3_clients: dict[str, Any],
    ) -> None:
        mock_boto3_clients["dynamodb_table"].update_item.side_effect = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException"}},
            "UpdateItem",
        )

        response = lambda_function_module.lambda_handler(sample_analyze_event, mock_lambda_context)

        body = assert_success_response(response, 202, required_fields=["job_id", "status_url"])
        assert body["message"] == "Analysis already queued"
        mock_boto3_clients["lambda"].invoke.assert_not_called()

    def test_rejects_mismatched_job_s3_key(
        self,
        lambda_function_module: Any,
        mock_lambda_context: Any,
    ) -> None:
        event = {
            "body": json.dumps(
                {
                    "job_id": "test-job-123",
                    "s3_url": "s3://test-resume-bucket/uploads/other-job/test-resume.pdf",
                }
            ),
            "isBase64Encoded": False,
        }

        response = lambda_function_module.lambda_handler(event, mock_lambda_context)
        assert_error_response(response, 400, "does not match")

    def test_rejects_wrong_bucket_in_job_record(
        self,
        lambda_function_module: Any,
        mock_lambda_context: Any,
        mock_boto3_clients: dict[str, Any],
    ) -> None:
        mock_boto3_clients["dynamodb_table"].get_item.return_value = {
            "Item": {
                "job_id": "test-job-123",
                "status": "upload_pending",
                "s3_bucket": "other-bucket",
                "s3_key": "uploads/test-job-123/test-resume.pdf",
            }
        }
        event = {"body": '{"job_id": "test-job-123"}', "isBase64Encoded": False}

        response = lambda_function_module.lambda_handler(event, mock_lambda_context)
        assert_error_response(response, 400, "unexpected bucket")

    def test_rejects_missing_job_record(
        self,
        lambda_function_module: Any,
        mock_lambda_context: Any,
        mock_boto3_clients: dict[str, Any],
    ) -> None:
        mock_boto3_clients["dynamodb_table"].get_item.return_value = {}

        response = lambda_function_module.lambda_handler(
            {"body": '{"job_id": "missing-job"}', "isBase64Encoded": False},
            mock_lambda_context,
        )
        assert_error_response(response, 404, "Job not found")

    def test_rejects_oversized_job_description(
        self,
        lambda_function_module: Any,
        mock_lambda_context: Any,
    ) -> None:
        event = {
            "body": json.dumps(
                {
                    "job_id": "test-job-123",
                    "job_description": "x"
                    * (lambda_function_module.MAX_JOB_DESCRIPTION_LENGTH + 1),
                }
            ),
            "isBase64Encoded": False,
        }

        response = lambda_function_module.lambda_handler(event, mock_lambda_context)
        assert_error_response(response, 400, "job_description exceeds")

    def test_rejects_bad_base64_body(
        self,
        lambda_function_module: Any,
        mock_lambda_context: Any,
    ) -> None:
        response = lambda_function_module.lambda_handler(
            {"body": "!!!!", "isBase64Encoded": True},
            mock_lambda_context,
        )
        assert_error_response(response, 400, "Invalid base64")

    def test_analyze_handles_non_json_gemini_output(
        self,
        lambda_function_module: Any,
        mock_lambda_context: Any,
        mock_boto3_clients: dict[str, Any],
    ) -> None:
        mock_boto3_clients["dynamodb_table"].get_item.return_value = {
            "Item": {
                "job_id": "test-job-123",
                "status": "queued",
                "s3_bucket": "test-resume-bucket",
                "s3_key": "uploads/test-job-123/test-resume.pdf",
            }
        }
        lambda_function_module._get_genai_client().models.generate_content.return_value.text = (
            "not json"
        )

        response = lambda_function_module.lambda_handler(
            {"source": lambda_function_module.INTERNAL_WORKER_SOURCE, "job_id": "test-job-123"},
            mock_lambda_context,
        )
        assert response["status"] == "failed"

    def test_analyze_handles_s3_client_error(
        self,
        lambda_function_module: Any,
        mock_lambda_context: Any,
        mock_boto3_clients: dict[str, Any],
    ) -> None:
        mock_boto3_clients["dynamodb_table"].get_item.return_value = {
            "Item": {
                "job_id": "test-job-123",
                "status": "queued",
                "s3_bucket": "test-resume-bucket",
                "s3_key": "uploads/test-job-123/test-resume.pdf",
            }
        }
        error = ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        lambda_function_module.get_s3_client().get_object.side_effect = error

        response = lambda_function_module.lambda_handler(
            {"source": lambda_function_module.INTERNAL_WORKER_SOURCE, "job_id": "test-job-123"},
            mock_lambda_context,
        )
        assert response["status"] == "failed"
        assert "NoSuchKey" in response["error"]

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

    def test_download_pdf_empty(
        self, lambda_function_module: Any, mock_boto3_clients: dict[str, Any]
    ) -> None:
        mock_body = MagicMock()
        mock_body.read.return_value = b""
        mock_boto3_clients["s3"].get_object.return_value = {"Body": mock_body}
        with pytest.raises(ValueError, match="Downloaded PDF is empty"):
            lambda_function_module._download_pdf_bytes("bucket", "key.pdf")

    def test_download_pdf_invalid_magic_bytes(
        self, lambda_function_module: Any, mock_boto3_clients: dict[str, Any]
    ) -> None:
        mock_body = MagicMock()
        mock_body.read.return_value = b"not-a-pdf"
        mock_boto3_clients["s3"].get_object.return_value = {"Body": mock_body}

        with pytest.raises(ValueError, match="valid PDF"):
            lambda_function_module._download_pdf_bytes("bucket", "key.pdf")

    def test_download_pdf_too_large(
        self, lambda_function_module: Any, mock_boto3_clients: dict[str, Any]
    ) -> None:
        mock_body = MagicMock()
        mock_body.read.return_value = b"%PDF" + (b"a" * lambda_function_module.MAX_PDF_SIZE)
        mock_boto3_clients["s3"].get_object.return_value = {"Body": mock_body}

        with pytest.raises(ValueError, match="maximum size"):
            lambda_function_module._download_pdf_bytes("bucket", "key.pdf")

    def test_analyze_missing_google_api_key(
        self, lambda_function_module: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        monkeypatch.delenv("GOOGLE_API_KEY_SECRET_ARN", raising=False)
        lambda_function_module._genai_client = None
        lambda_function_module._genai_client_api_key = None
        with pytest.raises(RuntimeError, match="GOOGLE_API_KEY"):
            lambda_function_module.analyze_resume_pdf(b"%PDF-1.4", "job desc")

    def test_analyze_empty_gemini_response(self, lambda_function_module: Any) -> None:
        mock_client = lambda_function_module._get_genai_client()
        mock_client.models.generate_content.return_value.text = None
        with pytest.raises(RuntimeError, match="empty response"):
            lambda_function_module.analyze_resume_pdf(b"%PDF-1.4", "job desc")

    def test_analyze_retries_service_unavailable(
        self,
        lambda_function_module: Any,
        mock_lambda_context: Any,
        mock_boto3_clients: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        mock_boto3_clients["dynamodb_table"].get_item.return_value = {
            "Item": {
                "job_id": "test-job-123",
                "status": "queued",
                "s3_bucket": "test-resume-bucket",
                "s3_key": "uploads/test-job-123/test-resume.pdf",
            }
        }
        sleep_calls: list[float] = []
        monkeypatch.setattr(
            lambda_function_module.time,
            "sleep",
            sleep_calls.append,
        )

        mock_client = lambda_function_module._get_genai_client()
        mock_client.models.generate_content.side_effect = [
            FakeServerUnavailableError(),
            FakeServerUnavailableError(),
            mock_client.models.generate_content.return_value,
        ]

        response = lambda_function_module.lambda_handler(
            {"source": lambda_function_module.INTERNAL_WORKER_SOURCE, "job_id": "test-job-123"},
            mock_lambda_context,
        )

        assert response["status"] == "completed"
        assert sleep_calls == [1.0, 2.0]
        assert mock_client.models.generate_content.call_count == 3

    def test_analyze_retries_rate_limit_errors(
        self,
        lambda_function_module: Any,
        mock_lambda_context: Any,
        mock_boto3_clients: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        mock_boto3_clients["dynamodb_table"].get_item.return_value = {
            "Item": {
                "job_id": "test-job-123",
                "status": "queued",
                "s3_bucket": "test-resume-bucket",
                "s3_key": "uploads/test-job-123/test-resume.pdf",
            }
        }
        sleep_calls: list[float] = []
        monkeypatch.setattr(
            lambda_function_module.time,
            "sleep",
            sleep_calls.append,
        )

        mock_client = lambda_function_module._get_genai_client()
        mock_client.models.generate_content.side_effect = [
            FakeRateLimitError(),
            mock_client.models.generate_content.return_value,
        ]

        response = lambda_function_module.lambda_handler(
            {"source": lambda_function_module.INTERNAL_WORKER_SOURCE, "job_id": "test-job-123"},
            mock_lambda_context,
        )

        assert response["status"] == "completed"
        assert sleep_calls == [1.0]
        assert mock_client.models.generate_content.call_count == 2

    @pytest.mark.parametrize(
        ("exc", "expected"),
        [
            (FakeServerUnavailableError(), True),
            (FakeRateLimitError(status_code=429), True),
            (
                FakeRateLimitError(
                    message="Quota exceeded because of rate limit", status_code=None
                ),
                True,
            ),
            (FakeRateLimitError(message="TooManyRequests", status_code=None, code="429"), True),
            (Exception("Completely different error"), False),
        ],
    )
    def test_retryable_upstream_error_detection(
        self,
        lambda_function_module: Any,
        exc: Exception,
        expected: bool,
    ) -> None:
        assert lambda_function_module._is_retryable_upstream_error(exc) is expected

    def test_analyze_uses_secret_manager_api_key(
        self,
        lambda_function_module: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        secret_client = MagicMock()
        secret_client.get_secret_value.return_value = {
            "SecretString": json.dumps({"GOOGLE_API_KEY": "secret-api-key"})
        }

        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        monkeypatch.setenv(
            "GOOGLE_API_KEY_SECRET_ARN",
            "arn:aws:secretsmanager:us-east-1:123456789012:secret:test",
        )

        lambda_function_module._genai_client = None
        lambda_function_module._genai_client_api_key = None
        lambda_function_module._cached_secret_arn = None
        lambda_function_module._cached_google_api_key = None
        monkeypatch.setattr(
            lambda_function_module,
            "_get_secrets_client",
            lambda: secret_client,
        )

        lambda_function_module._get_genai_client()

        assert lambda_function_module.Client.call_args[1]["api_key"] == "secret-api-key"

    def test_update_job_status_no_job_id(self, lambda_function_module: Any) -> None:
        lambda_function_module._update_job_status(None, "completed")

    def test_update_job_status_no_table(self, lambda_function_module: Any) -> None:
        original = lambda_function_module.get_results_table

        def _no_table():
            raise RuntimeError("not configured")

        lambda_function_module.get_results_table = _no_table
        try:
            lambda_function_module._update_job_status("job-123", "completed")
        finally:
            lambda_function_module.get_results_table = original

    def test_update_job_status_exception(
        self, lambda_function_module: Any, mock_boto3_clients: dict[str, Any]
    ) -> None:
        mock_boto3_clients["dynamodb_table"].update_item.side_effect = Exception("DB error")
        lambda_function_module._update_job_status("job-123", "completed")

    def test_get_job_record_no_table(self, lambda_function_module: Any) -> None:
        original = lambda_function_module.get_results_table

        def _no_table():
            raise RuntimeError("not configured")

        lambda_function_module.get_results_table = _no_table
        try:
            result = lambda_function_module._get_job_record("job-123")
            assert result == {}
        finally:
            lambda_function_module.get_results_table = original

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
            "Item": {
                "job_id": "job-123",
                "status": "upload_pending",
                "s3_bucket": "test-resume-bucket",
                "s3_key": "uploads/job-123/test-resume.pdf",
                "job_description": "From DB",
            }
        }
        event = {
            "body": '{"job_id": "job-123", "s3_url": "s3://test-resume-bucket/uploads/job-123/test-resume.pdf"}',
            "isBase64Encoded": False,
        }
        response = lambda_function_module.lambda_handler(event, mock_lambda_context)
        body = assert_success_response(response, 202, required_fields=["job_id", "status_url"])
        assert body["status"] == "queued"
        mock_boto3_clients["lambda"].invoke.assert_called_once()

    def test_returns_cached_analysis_when_job_already_completed(
        self,
        lambda_function_module: Any,
        mock_lambda_context: Any,
        mock_boto3_clients: dict[str, Any],
    ) -> None:
        mock_boto3_clients["dynamodb_table"].get_item.return_value = {
            "Item": {
                "job_id": "test-job-123",
                "status": "completed",
                "s3_bucket": "test-resume-bucket",
                "s3_key": "uploads/test-job-123/test-resume.pdf",
                "analysis_result": {"name": "Jane Doe", "education": []},
            }
        }
        event = {"body": '{"job_id": "test-job-123"}', "isBase64Encoded": False}

        response = lambda_function_module.lambda_handler(event, mock_lambda_context)
        body = assert_success_response(response, 200, required_fields=["status_url", "message"])

        assert body["message"] == "Analysis already completed"
        assert "analysis_result" not in body
        lambda_function_module.get_s3_client().get_object.assert_not_called()
        lambda_function_module._get_genai_client().models.generate_content.assert_not_called()

    def test_rejects_duplicate_processing_request(
        self,
        lambda_function_module: Any,
        mock_lambda_context: Any,
        mock_boto3_clients: dict[str, Any],
    ) -> None:
        mock_boto3_clients["dynamodb_table"].get_item.return_value = {
            "Item": {
                "job_id": "test-job-123",
                "status": "processing",
                "s3_bucket": "test-resume-bucket",
                "s3_key": "uploads/test-job-123/test-resume.pdf",
            }
        }
        event = {"body": '{"job_id": "test-job-123"}', "isBase64Encoded": False}

        response = lambda_function_module.lambda_handler(event, mock_lambda_context)
        body = assert_success_response(response, 202, required_fields=["status_url"])
        assert body["message"] == "Analysis already queued"
        mock_boto3_clients["lambda"].invoke.assert_not_called()

    def test_rejects_analysis_when_upload_not_ready(
        self,
        lambda_function_module: Any,
        mock_lambda_context: Any,
        mock_boto3_clients: dict[str, Any],
    ) -> None:
        mock_boto3_clients["dynamodb_table"].get_item.return_value = {
            "Item": {
                "job_id": "test-job-123",
                "status": "queued",
                "s3_bucket": "test-resume-bucket",
                "s3_key": "uploads/test-job-123/test-resume.pdf",
            }
        }
        lambda_function_module.get_s3_client().head_object.side_effect = ClientError(
            {"Error": {"Code": "404"}},
            "HeadObject",
        )

        response = lambda_function_module.lambda_handler(
            {"source": lambda_function_module.INTERNAL_WORKER_SOURCE, "job_id": "test-job-123"},
            mock_lambda_context,
        )
        assert response["status"] == "failed"
        assert "finish the upload" in response["error"]

    def test_status_update_processing(
        self,
        lambda_function_module: Any,
        mock_lambda_context: Any,
        mock_boto3_clients: dict[str, Any],
    ) -> None:
        mock_boto3_clients["dynamodb_table"].get_item.return_value = {
            "Item": {
                "job_id": "test-job-123",
                "status": "queued",
                "s3_bucket": "test-resume-bucket",
                "s3_key": "uploads/test-job-123/test-resume.pdf",
            }
        }
        lambda_function_module.lambda_handler(
            {"source": lambda_function_module.INTERNAL_WORKER_SOURCE, "job_id": "test-job-123"},
            mock_lambda_context,
        )
        calls = mock_boto3_clients["dynamodb_table"].update_item.call_args_list
        statuses = [c[1]["ExpressionAttributeValues"][":status"] for c in calls]
        assert "processing" in statuses
        assert "completed" in statuses

    def test_status_update_failed_on_s3_error(
        self,
        lambda_function_module: Any,
        mock_lambda_context: Any,
        mock_boto3_clients: dict[str, Any],
    ) -> None:
        from botocore.exceptions import ClientError

        mock_boto3_clients["dynamodb_table"].get_item.return_value = {
            "Item": {
                "job_id": "test-job-123",
                "status": "queued",
                "s3_bucket": "test-resume-bucket",
                "s3_key": "uploads/test-job-123/test-resume.pdf",
            }
        }
        mock_boto3_clients["s3"].get_object.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied"}}, "GetObject"
        )
        response = lambda_function_module.lambda_handler(
            {"source": lambda_function_module.INTERNAL_WORKER_SOURCE, "job_id": "test-job-123"},
            mock_lambda_context,
        )
        assert response["status"] == "failed"
        calls = mock_boto3_clients["dynamodb_table"].update_item.call_args_list
        statuses = [c[1]["ExpressionAttributeValues"][":status"] for c in calls]
        assert "failed" in statuses

    def test_status_update_failed_on_exception(
        self,
        lambda_function_module: Any,
        mock_lambda_context: Any,
        mock_boto3_clients: dict[str, Any],
    ) -> None:
        mock_boto3_clients["dynamodb_table"].get_item.return_value = {
            "Item": {
                "job_id": "test-job-123",
                "status": "queued",
                "s3_bucket": "test-resume-bucket",
                "s3_key": "uploads/test-job-123/test-resume.pdf",
            }
        }
        mock_boto3_clients["s3"].get_object.side_effect = Exception("Unexpected error")
        response = lambda_function_module.lambda_handler(
            {"source": lambda_function_module.INTERNAL_WORKER_SOURCE, "job_id": "test-job-123"},
            mock_lambda_context,
        )
        assert response["status"] == "failed"
        calls = mock_boto3_clients["dynamodb_table"].update_item.call_args_list
        statuses = [c[1]["ExpressionAttributeValues"][":status"] for c in calls]
        assert "failed" in statuses
