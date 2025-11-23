"""Unit tests for upload_handler.py."""

import base64
import json
from typing import Any

import pytest


@pytest.fixture
def upload_handler_module(mock_boto3_clients) -> Any:
    """Import upload_handler with mocked dependencies."""
    from resume_analyzer import upload_handler


    original_s3 = upload_handler.s3_client
    original_dynamodb = upload_handler.dynamodb
    original_sts = upload_handler.sts_client

    upload_handler.s3_client = mock_boto3_clients["s3"]
    upload_handler.dynamodb = mock_boto3_clients["dynamodb"]
    upload_handler.sts_client = mock_boto3_clients["sts"]

    try:
        yield upload_handler
    finally:
        upload_handler.s3_client = original_s3
        upload_handler.dynamodb = original_dynamodb
        upload_handler.sts_client = original_sts


class TestUploadHandler:
    """Test suite for upload_handler lambda function."""

    def test_successful_upload(
        self, upload_handler_module, sample_upload_event, mock_lambda_context, mock_boto3_clients
    ) -> None:
        """Test successful PDF upload and job creation."""
        response = upload_handler_module.lambda_handler(sample_upload_event, mock_lambda_context)

        assert response["statusCode"] == 202
        body = json.loads(response["body"])

        assert "job_id" in body
        assert body["status"] == "processing"
        assert body["message"] == "Resume uploaded successfully. Analysis in progress."
        assert "/analyze/" in body["poll_url"]


        mock_boto3_clients["s3"].put_object.assert_called_once()
        call_args = mock_boto3_clients["s3"].put_object.call_args[1]
        assert call_args["Bucket"] == "test-resume-bucket"
        assert call_args["ContentType"] == "application/pdf"
        assert "uploads/" in call_args["Key"]


        mock_boto3_clients["dynamodb_table"].put_item.assert_called_once()
        item_args = mock_boto3_clients["dynamodb_table"].put_item.call_args[1]["Item"]
        assert item_args["status"] == "processing"
        assert item_args["job_description"] == "Python developer position"
        assert item_args["filename"] == "test-resume.pdf"

    def test_missing_body(self, upload_handler_module, mock_lambda_context) -> None:
        """Test request without body."""
        event = {"httpMethod": "POST"}
        response = upload_handler_module.lambda_handler(event, mock_lambda_context)

        assert response["statusCode"] == 400
        body = json.loads(response["body"])
        assert "No body in request" in body["error"]

    def test_missing_pdf_base64(self, upload_handler_module, mock_lambda_context) -> None:
        """Test request without pdf_base64 field."""
        event = {"body": json.dumps({"filename": "test.pdf"}), "isBase64Encoded": False}
        response = upload_handler_module.lambda_handler(event, mock_lambda_context)

        assert response["statusCode"] == 400
        body = json.loads(response["body"])
        assert "Missing required field: pdf_base64" in body["error"]

    def test_invalid_json(self, upload_handler_module, mock_lambda_context) -> None:
        """Test request with invalid JSON."""
        event = {"body": "not valid json{", "isBase64Encoded": False}
        response = upload_handler_module.lambda_handler(event, mock_lambda_context)

        assert response["statusCode"] == 400
        body = json.loads(response["body"])
        assert "Invalid JSON" in body["error"]

    def test_invalid_base64_pdf(self, upload_handler_module, mock_lambda_context) -> None:
        """Test request with invalid base64 encoding."""
        event = {
            "body": json.dumps({"pdf_base64": "not-valid-base64!!!"}),
            "isBase64Encoded": False,
        }
        response = upload_handler_module.lambda_handler(event, mock_lambda_context)

        assert response["statusCode"] == 500
        body = json.loads(response["body"])
        assert "error" in body

    def test_base64_encoded_body(
        self, upload_handler_module, sample_pdf_base64, mock_lambda_context, mock_boto3_clients
    ) -> None:
        """Test with base64-encoded request body."""
        body_json = json.dumps({"pdf_base64": sample_pdf_base64, "filename": "test.pdf"})
        event = {"body": base64.b64encode(body_json.encode()).decode(), "isBase64Encoded": True}

        response = upload_handler_module.lambda_handler(event, mock_lambda_context)
        assert response["statusCode"] == 202

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
        assert response["statusCode"] == 202


        item_args = mock_boto3_clients["dynamodb_table"].put_item.call_args[1]["Item"]
        assert item_args["job_description"] == "General resume analysis"
        assert item_args["filename"] == "resume.pdf"

    def test_cors_headers(self, upload_handler_module, sample_upload_event, mock_lambda_context) -> None:
        """Test that CORS headers are present in all responses."""
        response = upload_handler_module.lambda_handler(sample_upload_event, mock_lambda_context)

        assert "Access-Control-Allow-Origin" in response["headers"]
        assert response["headers"]["Access-Control-Allow-Origin"] == "*"

    def test_s3_error_handling(
        self, upload_handler_module, sample_upload_event, mock_lambda_context, mock_boto3_clients
    ) -> None:
        """Test error handling when S3 upload fails."""
        mock_boto3_clients["s3"].put_object.side_effect = Exception("S3 error")

        response = upload_handler_module.lambda_handler(sample_upload_event, mock_lambda_context)
        assert response["statusCode"] == 500
        body = json.loads(response["body"])
        assert "error" in body

    def test_dynamodb_error_handling(
        self, upload_handler_module, sample_upload_event, mock_lambda_context, mock_boto3_clients
    ) -> None:
        """Test error handling when DynamoDB write fails."""
        mock_boto3_clients["dynamodb_table"].put_item.side_effect = Exception("DynamoDB error")

        response = upload_handler_module.lambda_handler(sample_upload_event, mock_lambda_context)
        assert response["statusCode"] == 500
        body = json.loads(response["body"])
        assert "error" in body

    def test_account_id_caching(self, upload_handler_module, mock_boto3_clients) -> None:
        """Test that AWS account ID is cached."""
        upload_handler_module.get_aws_account_id.cache_clear()

        id1 = upload_handler_module.get_aws_account_id()
        id2 = upload_handler_module.get_aws_account_id()

        assert id1 == id2 == "123456789012"

        assert mock_boto3_clients["sts"].get_caller_identity.call_count == 1

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
        assert response["statusCode"] == 202


        call_args = mock_boto3_clients["s3"].put_object.call_args[1]
        metadata = call_args["Metadata"]
        assert len(metadata["job_description"]) <= 2000
        assert len(metadata["filename"]) <= 256
