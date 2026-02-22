"""Unit tests for upload_handler.py."""

import json
from typing import Any

import pytest

from tests.test_utils import assert_error_response, assert_success_response


@pytest.fixture
def upload_handler_module(mock_boto3_clients) -> Any:
    """Import upload_handler with mocked dependencies."""
    from resume_analyzer import upload_handler

    original_s3 = upload_handler.s3_client
    original_dynamodb = upload_handler.dynamodb
    original_results_table = upload_handler.results_table

    upload_handler.s3_client = mock_boto3_clients["s3"]
    upload_handler.dynamodb = mock_boto3_clients["dynamodb"]
    upload_handler.results_table = mock_boto3_clients["dynamodb_table"]

    try:
        yield upload_handler
    finally:
        upload_handler.s3_client = original_s3
        upload_handler.dynamodb = original_dynamodb
        upload_handler.results_table = original_results_table


@pytest.mark.integration
@pytest.mark.aws
class TestUploadHandler:
    """Tests for S3 presigned upload URL generation."""

    def test_generate_presigned_upload(
        self, upload_handler_module, sample_upload_event, mock_lambda_context, mock_boto3_clients
    ) -> None:
        response = upload_handler_module.lambda_handler(sample_upload_event, mock_lambda_context)

        body = assert_success_response(
            response,
            200,
            required_fields=["job_id", "status", "s3_key", "s3_url", "upload", "expires_in"],
        )

        assert body["status"] == "upload_pending"
        assert body["s3_key"].startswith("uploads/")
        assert body["s3_url"].startswith("s3://test-resume-bucket/uploads/")
        assert body["upload"]["url"] == "https://test-resume-bucket.s3.amazonaws.com"
        assert "fields" in body["upload"]

        mock_boto3_clients["s3"].generate_presigned_post.assert_called_once()
        call_args = mock_boto3_clients["s3"].generate_presigned_post.call_args[1]
        assert call_args["Bucket"] == "test-resume-bucket"
        assert call_args["Key"].startswith("uploads/")

        mock_boto3_clients["dynamodb_table"].put_item.assert_called_once()
        item = mock_boto3_clients["dynamodb_table"].put_item.call_args[1]["Item"]
        assert item["status"] == "upload_pending"
        assert item["filename"] == "test-resume.pdf"

    def test_sanitizes_filename(
        self, upload_handler_module, mock_lambda_context, mock_boto3_clients
    ) -> None:
        event = {
            "body": json.dumps({"filename": "bad\nname?.pdf"}),
            "isBase64Encoded": False,
        }

        response = upload_handler_module.lambda_handler(event, mock_lambda_context)
        body = assert_success_response(response, 200)

        assert body["s3_key"].endswith("bad_name_.pdf")

    @pytest.mark.parametrize(
        ("event", "message"),
        [
            ({"httpMethod": "POST"}, "No body in request"),
            ({"body": "not-json", "isBase64Encoded": False}, "Invalid JSON format"),
        ],
    )
    def test_bad_requests(self, upload_handler_module, mock_lambda_context, event, message) -> None:
        response = upload_handler_module.lambda_handler(event, mock_lambda_context)
        assert_error_response(response, 400, message)

    def test_presigned_generation_failure(
        self, upload_handler_module, sample_upload_event, mock_lambda_context, mock_boto3_clients
    ) -> None:
        mock_boto3_clients["s3"].generate_presigned_post.side_effect = Exception("S3 failure")

        response = upload_handler_module.lambda_handler(sample_upload_event, mock_lambda_context)
        assert_error_response(response, 500, "S3 failure")

    def test_sanitize_filename_adds_pdf_extension(self, upload_handler_module) -> None:
        result = upload_handler_module._sanitize_filename("resume")
        assert result.endswith(".pdf")

    def test_sanitize_filename_empty_string(self, upload_handler_module) -> None:
        result = upload_handler_module._sanitize_filename("")
        assert result == "resume.pdf"

    def test_resume_bucket_not_configured(self, upload_handler_module, mock_lambda_context) -> None:
        original = upload_handler_module.RESUME_BUCKET
        upload_handler_module.RESUME_BUCKET = None
        try:
            event = {"body": '{"filename": "test.pdf"}', "isBase64Encoded": False}
            response = upload_handler_module.lambda_handler(event, mock_lambda_context)
            assert_error_response(response, 500, "RESUME_BUCKET")
        finally:
            upload_handler_module.RESUME_BUCKET = original

    def test_results_table_not_configured(self, upload_handler_module, mock_lambda_context) -> None:
        original = upload_handler_module.results_table
        upload_handler_module.results_table = None
        try:
            event = {"body": '{"filename": "test.pdf"}', "isBase64Encoded": False}
            response = upload_handler_module.lambda_handler(event, mock_lambda_context)
            assert_error_response(response, 500, "RESULTS_TABLE")
        finally:
            upload_handler_module.results_table = original

    def test_base64_encoded_body(
        self, upload_handler_module, mock_lambda_context, mock_boto3_clients
    ) -> None:
        import base64

        payload = json.dumps({"filename": "test.pdf", "job_description": "test"})
        encoded = base64.b64encode(payload.encode()).decode()
        event = {"body": encoded, "isBase64Encoded": True}
        response = upload_handler_module.lambda_handler(event, mock_lambda_context)
        body = assert_success_response(response, 200, required_fields=["job_id", "s3_key"])
        assert "test.pdf" in body["s3_key"]

    def test_body_not_dict(self, upload_handler_module, mock_lambda_context) -> None:
        event = {"body": "[]", "isBase64Encoded": False}
        response = upload_handler_module.lambda_handler(event, mock_lambda_context)
        assert_error_response(response, 400, "Invalid JSON body")
