"""Unit tests for lambda_function.py (main analyzer)."""

import base64
import importlib
import json
from typing import Any
from unittest.mock import MagicMock, Mock

import pytest
from botocore.exceptions import ClientError


@pytest.fixture
def lambda_function_module(mock_boto3_clients: dict[str, Any]) -> Any:
    """Import lambda_function with mocked dependencies."""
    from resume_analyzer import lambda_function

    # Patch the global clients
    original_s3 = lambda_function.s3_client
    original_dynamodb = lambda_function.dynamodb
    original_sts = lambda_function.sts_client

    lambda_function.s3_client = mock_boto3_clients["s3"]
    lambda_function.dynamodb = mock_boto3_clients["dynamodb"]
    lambda_function.sts_client = mock_boto3_clients["sts"]

    # Mock the Gemini LLM
    mock_llm = MagicMock()
    mock_response = MagicMock()
    mock_response.content = (
        "## Key Strengths\n- Python expert\n\n"
        "## Gaps & Areas for Development\n- Limited leadership\n\n"
        "## Recommendations\n- Take leadership courses"
    )
    mock_llm.invoke.return_value = mock_response
    original_gemini = lambda_function.gemini_llm
    lambda_function.gemini_llm = mock_llm

    try:
        # Ensure api_key is set
        lambda_function.api_key = "test-api-key-123"
        # Clear caches and force mocks
        lambda_function.get_aws_account_id.cache_clear()
        yield lambda_function
    finally:
        lambda_function.s3_client = original_s3
        lambda_function.dynamodb = original_dynamodb
        lambda_function.sts_client = original_sts
        lambda_function.gemini_llm = original_gemini


class TestReadPDF:
    """Test suite for PDF reading functions."""

    def test_read_pdf_from_bytes_success(self, lambda_function_module: Any, sample_pdf_base64: str) -> None:
        """Test successful PDF text extraction from bytes."""
        pdf_bytes = base64.b64decode(sample_pdf_base64)
        result = lambda_function_module.read_pdf_from_bytes(pdf_bytes)

        assert isinstance(result, str)
        assert len(result.strip()) > 0
        assert "error" not in result.lower()

    def test_read_pdf_from_bytes_invalid(self, lambda_function_module: Any) -> None:
        """Test PDF reading with invalid bytes."""
        result = lambda_function_module.read_pdf_from_bytes(b"not a valid pdf")

        assert "error occurred" in result.lower()

    def test_read_pdf_from_bytes_empty(self, lambda_function_module: Any) -> None:
        """Test PDF reading with empty content."""
        # Create a minimal valid PDF with no actual text
        empty_pdf = b"%PDF-1.4\n%%EOF"
        result = lambda_function_module.read_pdf_from_bytes(empty_pdf)

        assert "Could not extract text" in result or "error" in result.lower()

    def test_read_pdf_from_s3_success(
        self, lambda_function_module: Any, mock_boto3_clients: dict[str, Any], sample_pdf_base64: str
    ) -> None:
        """Test successful PDF reading from S3."""
        pdf_bytes = base64.b64decode(sample_pdf_base64)
        mock_boto3_clients["s3"].get_object.return_value = {"Body": Mock(read=lambda: pdf_bytes)}

        result = lambda_function_module.read_pdf_from_s3("test-bucket", "test-key.pdf")

        assert isinstance(result, str)
        assert len(result.strip()) > 0
        assert "error" not in result.lower()

    def test_read_pdf_from_s3_not_found(self, lambda_function_module: Any, mock_boto3_clients: dict[str, Any]) -> None:
        """Test PDF reading when file doesn't exist in S3."""
        error = ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        mock_boto3_clients["s3"].get_object.side_effect = error

        result = lambda_function_module.read_pdf_from_s3("test-bucket", "missing.pdf")

        assert "not found" in result.lower()

    def test_read_pdf_from_s3_access_denied(self, lambda_function_module: Any, mock_boto3_clients: dict[str, Any]) -> None:
        """Test PDF reading with access denied error."""
        error = ClientError({"Error": {"Code": "AccessDenied"}}, "GetObject")
        mock_boto3_clients["s3"].get_object.side_effect = error

        result = lambda_function_module.read_pdf_from_s3("test-bucket", "test.pdf")

        assert "ClientError" in result


class TestAnalyzeResume:
    """Test suite for resume analysis function."""

    def test_analyze_resume_success(self, lambda_function_module: Any) -> None:
        """Test successful resume analysis."""
        resume = "John Doe\nPython Developer\n5 years experience"
        job_desc = "Looking for Python developer with 3+ years"

        result = lambda_function_module.analyze_resume(resume, job_desc)

        assert isinstance(result, str)
        assert len(result) > 0
        assert "## Key Strengths" in result or "Python" in result

    def test_analyze_resume_no_api_key(self, lambda_function_module: Any) -> None:
        """Test analysis when API key is not configured."""
        # Temporarily remove gemini_llm
        original_llm = lambda_function_module.gemini_llm
        lambda_function_module.gemini_llm = None

        result = lambda_function_module.analyze_resume("resume", "job desc")

        assert "Error" in result
        assert "not initialized" in result

        # Restore
        lambda_function_module.gemini_llm = original_llm

    def test_analyze_resume_llm_error(self, lambda_function_module) -> None:
        """Test analysis when LLM throws an error."""
        lambda_function_module.gemini_llm.invoke.side_effect = Exception("API rate limit exceeded")

        result = lambda_function_module.analyze_resume("resume", "job desc")

        assert "Error during analysis" in result


class TestS3Operations:
    """Test suite for S3 operations."""

    def test_save_pdf_to_s3_success(self, lambda_function_module, mock_boto3_clients) -> None:
        """Test successful PDF save to S3."""
        pdf_bytes = b"%PDF-1.4\ntest content\n%%EOF"
        result = lambda_function_module.save_pdf_to_s3(pdf_bytes, "test-resume.pdf")

        assert result is not None
        assert "uploads/" in result
        assert "test-resume.pdf" in result

        # Verify S3 was called
        mock_boto3_clients["s3"].put_object.assert_called_once()
        call_args = mock_boto3_clients["s3"].put_object.call_args[1]
        assert call_args["ContentType"] == "application/pdf"
        assert call_args["Bucket"] == "test-resume-bucket"

    def test_save_pdf_to_s3_no_bucket(self, lambda_function_module, monkeypatch) -> None:
        """Test save PDF when bucket env var is not set."""
        monkeypatch.delenv("RESUME_BUCKET", raising=False)
        # Need to reload the module to pick up the env change

        importlib.reload(lambda_function_module)

        result = lambda_function_module.save_pdf_to_s3(b"test", "test.pdf")
        assert result is None

    def test_save_pdf_to_s3_error(self, lambda_function_module, mock_boto3_clients) -> None:
        """Test save PDF when S3 operation fails."""
        mock_boto3_clients["s3"].put_object.side_effect = Exception("S3 error")

        result = lambda_function_module.save_pdf_to_s3(b"test", "test.pdf")
        assert result is None

    def test_get_s3_metadata_success(self, lambda_function_module, mock_boto3_clients) -> None:
        """Test retrieving S3 metadata."""
        mock_boto3_clients["s3"].head_object.return_value = {
            "Metadata": {"job_id": "123", "job_description": "Test job"}
        }

        result = lambda_function_module._get_s3_metadata("bucket", "key")

        assert result["job_id"] == "123"
        assert result["job_description"] == "Test job"

    def test_get_s3_metadata_error(self, lambda_function_module, mock_boto3_clients) -> None:
        """Test S3 metadata retrieval with error."""
        mock_boto3_clients["s3"].head_object.side_effect = Exception("Error")

        result = lambda_function_module._get_s3_metadata("bucket", "key")

        assert result == {}


class TestDynamoDBOperations:
    """Test suite for DynamoDB operations."""

    def test_update_job_status_completed(self, lambda_function_module, mock_boto3_clients) -> None:
        """Test updating job status to completed."""
        lambda_function_module._update_job_status(
            "test-job-123", "completed", result="Analysis complete"
        )

        mock_boto3_clients["dynamodb_table"].update_item.assert_called_once()
        call_args = mock_boto3_clients["dynamodb_table"].update_item.call_args[1]
        assert call_args["Key"]["job_id"] == "test-job-123"
        assert ":result" in call_args["ExpressionAttributeValues"]
        assert ":completed" in call_args["ExpressionAttributeValues"]

    def test_update_job_status_failed(self, lambda_function_module, mock_boto3_clients) -> None:
        """Test updating job status to failed."""
        lambda_function_module._update_job_status(
            "test-job-123", "failed", error_msg="PDF parsing error"
        )

        mock_boto3_clients["dynamodb_table"].update_item.assert_called_once()
        call_args = mock_boto3_clients["dynamodb_table"].update_item.call_args[1]
        assert ":error" in call_args["ExpressionAttributeValues"]

    def test_update_job_status_no_job_id(self, lambda_function_module, mock_boto3_clients) -> None:
        """Test update with no job_id (should not crash)."""
        lambda_function_module._update_job_status(None, "completed")

        # Should not call DynamoDB
        mock_boto3_clients["dynamodb_table"].update_item.assert_not_called()

    def test_update_job_status_dynamodb_error(self, lambda_function_module, mock_boto3_clients) -> None:
        """Test update when DynamoDB fails (should not crash)."""
        mock_boto3_clients["dynamodb_table"].update_item.side_effect = Exception("DynamoDB error")

        # Should not raise exception
        lambda_function_module._update_job_status("test-job", "completed")


class TestEventParsing:
    """Test suite for event parsing functions."""

    def test_parse_s3_event_success(
        self, lambda_function_module, sample_s3_event, mock_boto3_clients
    ) -> None:
        """Test parsing valid S3 event."""
        mock_boto3_clients["s3"].head_object.return_value = {
            "Metadata": {"job_id": "test-123", "job_description": "Python dev"}
        }

        data, error = lambda_function_module._parse_s3_event(sample_s3_event)

        assert error is None
        assert data is not None
        assert data["s3_bucket"] == "test-resume-bucket"
        assert "uploads/" in data["s3_key"]
        assert data["job_id"] == "test-123"

    def test_parse_s3_event_invalid(self, lambda_function_module) -> None:
        """Test parsing invalid S3 event."""
        invalid_event = {"Records": [{"invalid": "structure"}]}

        data, error = lambda_function_module._parse_s3_event(invalid_event)

        assert data is None
        assert error is not None
        assert "Error parsing" in error

    def test_process_api_body_with_base64(
        self, lambda_function_module, sample_pdf_base64, mock_boto3_clients
    ) -> None:
        """Test processing API body with base64 PDF."""
        body = {
            "pdf_base64": sample_pdf_base64,
            "filename": "test.pdf",
            "job_description": "Python developer",
        }

        data, error = lambda_function_module._process_api_body(body)

        assert error is None
        assert data is not None
        assert data["job_description"] == "Python developer"
        assert data["resume_content"] is not None

    def test_process_api_body_with_s3_location(
        self, lambda_function_module, mock_boto3_clients, sample_pdf_base64
    ) -> None:
        """Test processing API body with S3 location."""
        pdf_bytes = base64.b64decode(sample_pdf_base64)
        mock_boto3_clients["s3"].get_object.return_value = {"Body": Mock(read=lambda: pdf_bytes)}

        body = {"s3_bucket": "test-bucket", "s3_key": "test-key.pdf", "job_description": "Test job"}

        data, error = lambda_function_module._process_api_body(body)

        assert error is None
        assert data is not None
        assert data["s3_bucket"] == "test-bucket"
        assert data["s3_key"] == "test-key.pdf"

    def test_process_api_body_invalid_base64(self, lambda_function_module) -> None:
        """Test processing API body with invalid base64."""
        body = {"pdf_base64": "not-valid-base64!!!"}

        data, error = lambda_function_module._process_api_body(body)

        assert data is None
        assert error is not None
        assert "base64" in error.lower()

    def test_process_api_body_missing_required(self, lambda_function_module) -> None:
        """Test processing API body without required fields."""
        body = {"job_description": "Test job"}

        data, error = lambda_function_module._process_api_body(body)

        assert data is None
        assert error is not None
        assert "must contain" in error

    def test_extract_event_data_s3(
        self, lambda_function_module, sample_s3_event, mock_boto3_clients
    ) -> None:
        """Test extracting data from S3 event."""
        mock_boto3_clients["s3"].head_object.return_value = {
            "Metadata": {"job_id": "123", "job_description": "Test"}
        }

        data, error = lambda_function_module._extract_event_data(sample_s3_event)

        assert error is None
        assert data["s3_bucket"] == "test-resume-bucket"

    def test_extract_event_data_api(self, lambda_function_module, sample_pdf_base64) -> None:
        """Test extracting data from API event."""
        event = {"body": json.dumps({"pdf_base64": sample_pdf_base64, "filename": "test.pdf"})}

        data, error = lambda_function_module._extract_event_data(event)

        assert error is None
        assert data["resume_content"] is not None

    def test_extract_event_data_invalid_json(self, lambda_function_module) -> None:
        """Test extracting data with invalid JSON."""
        event = {"body": "not valid json{"}

        _data, error = lambda_function_module._extract_event_data(event)

        assert error is not None
        assert "Invalid JSON" in error

    def test_extract_event_data_no_body(self, lambda_function_module) -> None:
        """Test extracting data from event without body."""
        event = {"httpMethod": "POST"}

        _data, error = lambda_function_module._extract_event_data(event)

        assert error is not None
        assert "No body" in error


class TestLambdaHandler:
    """Test suite for main lambda handler."""

    def test_handler_success_with_s3_event(
        self,
        lambda_function_module,
        sample_s3_event,
        mock_lambda_context,
        mock_boto3_clients,
        sample_pdf_base64,
    ) -> None:
        """Test successful processing of S3 event."""
        pdf_bytes = base64.b64decode(sample_pdf_base64)
        mock_boto3_clients["s3"].get_object.return_value = {"Body": Mock(read=lambda: pdf_bytes)}
        mock_boto3_clients["s3"].head_object.return_value = {
            "Metadata": {"job_id": "test-123", "job_description": "Python dev"}
        }

        response = lambda_function_module.lambda_handler(sample_s3_event, mock_lambda_context)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["message"] == "Success"
        assert "analysis_result" in body

    def test_handler_success_with_api_event(
        self, lambda_function_module, sample_pdf_base64, mock_lambda_context, mock_boto3_clients
    ) -> None:
        """Test successful processing of API event."""
        event = {
            "body": json.dumps(
                {
                    "pdf_base64": sample_pdf_base64,
                    "filename": "test.pdf",
                    "job_description": "Python developer",
                    "job_id": "test-job-456",
                }
            )
        }

        response = lambda_function_module.lambda_handler(event, mock_lambda_context)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert "analysis_result" in body
        assert body["job_id"] == "test-job-456"

    def test_handler_no_api_key(
        self, lambda_function_module, sample_pdf_base64, mock_lambda_context, monkeypatch
    ) -> None:
        """Test handler when API key is not set."""
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        # Reload to pick up env change

        importlib.reload(lambda_function_module)

        event = {"body": json.dumps({"pdf_base64": sample_pdf_base64})}
        response = lambda_function_module.lambda_handler(event, mock_lambda_context)

        assert response["statusCode"] == 500
        body = json.loads(response["body"])
        assert "API key not configured" in body["error"]

    def test_handler_invalid_event(self, lambda_function_module, mock_lambda_context) -> None:
        """Test handler with invalid event structure."""
        event = {"body": "not valid json{"}

        response = lambda_function_module.lambda_handler(event, mock_lambda_context)

        assert response["statusCode"] == 400
        body = json.loads(response["body"])
        assert "error" in body

    def test_handler_pdf_extraction_error(self, lambda_function_module, mock_lambda_context) -> None:
        """Test handler when PDF extraction fails."""
        event = {"body": json.dumps({"pdf_base64": "invalid base64", "job_id": "test-job"})}

        response = lambda_function_module.lambda_handler(event, mock_lambda_context)

        assert response["statusCode"] == 400 or response["statusCode"] == 500

    def test_handler_updates_dynamodb_on_completion(
        self, lambda_function_module, sample_pdf_base64, mock_lambda_context, mock_boto3_clients
    ) -> None:
        """Test that handler updates DynamoDB when job completes."""
        event = {"body": json.dumps({"pdf_base64": sample_pdf_base64, "job_id": "test-job-789"})}

        response = lambda_function_module.lambda_handler(event, mock_lambda_context)

        assert response["statusCode"] == 200
        # Verify DynamoDB update was called
        mock_boto3_clients["dynamodb_table"].update_item.assert_called()

    def test_handler_cors_headers(
        self, lambda_function_module, sample_pdf_base64, mock_lambda_context, mock_boto3_clients
    ) -> None:
        """Test that handler includes CORS headers."""
        event = {"body": json.dumps({"pdf_base64": sample_pdf_base64})}

        response = lambda_function_module.lambda_handler(event, mock_lambda_context)

        assert "Access-Control-Allow-Origin" in response["headers"]
        assert response["headers"]["Access-Control-Allow-Origin"] == "*"

    def test_handler_unexpected_exception(
        self, lambda_function_module, sample_pdf_base64, mock_lambda_context
    ) -> None:
        """Test handler with unexpected exception during analysis."""
        lambda_function_module.gemini_llm.invoke.side_effect = Exception("Unexpected error")

        event = {"body": json.dumps({"pdf_base64": sample_pdf_base64, "job_id": "test-job"})}

        response = lambda_function_module.lambda_handler(event, mock_lambda_context)

        # Should not crash, return error response
        assert response["statusCode"] == 200  # Still returns 200 but with error in analysis_result
        body = json.loads(response["body"])
        assert "Error during analysis" in body["analysis_result"]


class TestUtilityFunctions:
    """Test suite for utility functions."""

    def test_get_aws_account_id_success(self, lambda_function_module, mock_boto3_clients) -> None:
        """Test successful account ID retrieval."""
        lambda_function_module.get_aws_account_id.cache_clear()

        account_id = lambda_function_module.get_aws_account_id()

        assert account_id == "123456789012"

    def test_get_aws_account_id_error(self, lambda_function_module, mock_boto3_clients) -> None:
        """Test account ID retrieval with error."""
        lambda_function_module.get_aws_account_id.cache_clear()
        # Set side effect before calling
        mock_boto3_clients["sts"].get_caller_identity.side_effect = Exception("STS error")

        account_id = lambda_function_module.get_aws_account_id()

        assert account_id == ""

    def test_get_aws_account_id_caching(self, lambda_function_module, mock_boto3_clients) -> None:
        """Test that account ID is cached."""
        lambda_function_module.get_aws_account_id.cache_clear()

        id1 = lambda_function_module.get_aws_account_id()
        id2 = lambda_function_module.get_aws_account_id()

        assert id1 == id2
        # Should only be called once due to LRU cache
        assert mock_boto3_clients["sts"].get_caller_identity.call_count == 1
