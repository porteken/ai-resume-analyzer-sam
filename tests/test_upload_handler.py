"""Unit tests for upload_handler.py."""

import base64
import json
from typing import Any

import pytest

from tests.test_utils import (
    assert_error_response,
    assert_success_response,
)


@pytest.fixture
def upload_handler_module(mock_boto3_clients) -> Any:
    """Import upload_handler with mocked dependencies."""
    from resume_analyzer import upload_handler


    original_s3 = upload_handler.s3_client
    original_dynamodb = upload_handler.dynamodb
    original_results_table = upload_handler.results_table
    original_account_id = upload_handler.ACCOUNT_ID

    upload_handler.s3_client = mock_boto3_clients["s3"]
    upload_handler.dynamodb = mock_boto3_clients["dynamodb"]
    upload_handler.results_table = mock_boto3_clients["dynamodb_table"]
    upload_handler.ACCOUNT_ID = "123456789012"

    try:
        yield upload_handler
    finally:
        upload_handler.s3_client = original_s3
        upload_handler.dynamodb = original_dynamodb
        upload_handler.results_table = original_results_table
        upload_handler.ACCOUNT_ID = original_account_id


@pytest.mark.integration
@pytest.mark.aws
class TestUploadHandler:
    """Test suite for upload_handler lambda function."""

    def test_successful_upload(
        self, upload_handler_module, sample_upload_event, mock_lambda_context, mock_boto3_clients
    ) -> None:
        """Test successful PDF upload and job creation."""
        response = upload_handler_module.lambda_handler(sample_upload_event, mock_lambda_context)

        body = assert_success_response(
            response, 202, required_fields=["job_id", "status", "message", "poll_url"]
        )

        assert body["status"] == "processing"
        assert body["message"] == "Resume uploaded successfully. Analysis in progress."
        assert "/status/" in body["poll_url"]

        mock_boto3_clients["s3"].put_object.assert_called_once()
        call_args = mock_boto3_clients["s3"].put_object.call_args[1]
        assert call_args["Bucket"] == "test-resume-bucket"
        assert call_args["ContentType"] == "application/pdf"
        assert "uploads/" in call_args["Key"]
        assert call_args["ExpectedBucketOwner"] == "123456789012"

        mock_boto3_clients["dynamodb_table"].put_item.assert_called_once()
        item_args = mock_boto3_clients["dynamodb_table"].put_item.call_args[1]["Item"]
        assert item_args["status"] == "processing"
        assert item_args["job_description"] == "Python developer position"
        assert item_args["filename"] == "test-resume.pdf"

    @pytest.mark.parametrize(
        ("event", "expected_error"),
        [
            ({"httpMethod": "POST"}, "No body in request"),
            (
                {"body": json.dumps({"filename": "test.pdf"}), "isBase64Encoded": False},
                "Missing required field: pdf_base64",
            ),
            ({"body": "not valid json{", "isBase64Encoded": False}, "Invalid JSON"),
        ],
    )
    def test_invalid_requests(
        self, upload_handler_module, mock_lambda_context, event, expected_error
    ) -> None:
        """Test various invalid request scenarios."""
        response = upload_handler_module.lambda_handler(event, mock_lambda_context)
        assert_error_response(response, 400, expected_error)

    def test_invalid_base64_pdf(self, upload_handler_module, mock_lambda_context) -> None:
        """Test request with invalid base64 encoding."""
        event = {
            "body": json.dumps({"pdf_base64": "not-valid-base64!!!"}),
            "isBase64Encoded": False,
        }
        response = upload_handler_module.lambda_handler(event, mock_lambda_context)
        assert_error_response(response, 500)

    def test_base64_encoded_body(
        self, upload_handler_module, sample_pdf_base64, mock_lambda_context, mock_boto3_clients
    ) -> None:
        """Test with base64-encoded request body."""
        body_json = json.dumps({"pdf_base64": sample_pdf_base64, "filename": "test.pdf"})
        event = {"body": base64.b64encode(body_json.encode()).decode(), "isBase64Encoded": True}

        response = upload_handler_module.lambda_handler(event, mock_lambda_context)
        assert_success_response(response, 202, required_fields=["job_id", "status"])

    def test_metadata_sanitization(
        self, upload_handler_module, sample_pdf_base64, mock_lambda_context, mock_boto3_clients
    ) -> None:
        """Test that newlines are sanitized in S3 metadata."""
        job_desc_with_newlines = "Line 1\nLine 2\rLine 3\r\nLine 4"
        event = {
            "body": json.dumps(
                {
                    "pdf_base64": sample_pdf_base64,
                    "filename": "test\n.pdf",
                    "job_description": job_desc_with_newlines,
                }
            ),
            "isBase64Encoded": False,
        }

        response = upload_handler_module.lambda_handler(event, mock_lambda_context)
        assert response["statusCode"] == 202

        call_args = mock_boto3_clients["s3"].put_object.call_args[1]
        metadata = call_args["Metadata"]
        assert "\n" not in metadata["job_description"]
        assert "\r" not in metadata["job_description"]
        assert "\n" not in metadata["filename"]


        item_args = mock_boto3_clients["dynamodb_table"].put_item.call_args[1]["Item"]
        assert item_args["job_description"] == job_desc_with_newlines

    def test_default_values(
        self, upload_handler_module, sample_pdf_base64, mock_lambda_context, mock_boto3_clients
    ) -> None:
        """Test default values for optional fields."""
        event = {"body": json.dumps({"pdf_base64": sample_pdf_base64}), "isBase64Encoded": False}

        response = upload_handler_module.lambda_handler(event, mock_lambda_context)
        assert_success_response(response, 202)

        item_args = mock_boto3_clients["dynamodb_table"].put_item.call_args[1]["Item"]
        assert item_args["job_description"] == "General resume analysis"
        assert item_args["filename"] == "resume.pdf"

    def test_s3_error_handling(
        self, upload_handler_module, sample_upload_event, mock_lambda_context, mock_boto3_clients
    ) -> None:
        """Test error handling when S3 upload fails."""
        mock_boto3_clients["s3"].put_object.side_effect = Exception("S3 error")
        response = upload_handler_module.lambda_handler(sample_upload_event, mock_lambda_context)
        assert_error_response(response, 500)

    def test_dynamodb_error_handling(
        self, upload_handler_module, sample_upload_event, mock_lambda_context, mock_boto3_clients
    ) -> None:
        """Test error handling when DynamoDB write fails."""
        mock_boto3_clients["dynamodb_table"].put_item.side_effect = Exception("DynamoDB error")
        response = upload_handler_module.lambda_handler(sample_upload_event, mock_lambda_context)
        assert_error_response(response, 500)

    def test_long_metadata_truncation(
        self, upload_handler_module, sample_pdf_base64, mock_lambda_context, mock_boto3_clients
    ) -> None:
        """Test that long metadata values are truncated."""
        long_description = "x" * 3000
        long_filename = "y" * 300

        event = {
            "body": json.dumps(
                {
                    "pdf_base64": sample_pdf_base64,
                    "filename": long_filename,
                    "job_description": long_description,
                }
            ),
            "isBase64Encoded": False,
        }

        response = upload_handler_module.lambda_handler(event, mock_lambda_context)
        assert_success_response(response, 202)

        call_args = mock_boto3_clients["s3"].put_object.call_args[1]
        metadata = call_args["Metadata"]
        assert len(metadata["job_description"]) <= 2000
        assert len(metadata["filename"]) <= 256
