"""Unit tests for status_handler.py."""

import json
from typing import Any

import pytest


@pytest.fixture
def status_handler_module(mock_boto3_clients) -> Any:
    """Import status_handler with mocked dependencies."""
    from resume_analyzer import status_handler


    original_dynamodb = status_handler.dynamodb
    status_handler.dynamodb = mock_boto3_clients["dynamodb"]

    try:
        yield status_handler
    finally:
        status_handler.dynamodb = original_dynamodb


class TestStatusHandler:
    """Test suite for status_handler lambda function."""

    def test_get_completed_job(
        self, status_handler_module, sample_status_event, mock_lambda_context, mock_boto3_clients
    ) -> None:
        """Test retrieving a completed job."""
        mock_boto3_clients["dynamodb_table"].get_item.return_value = {
            "Item": {
                "job_id": "test-job-123",
                "status": "completed",
                "filename": "test-resume.pdf",
                "created_at": "2025-11-22T17:00:00",
                "analysis_result": "## Key Strengths\n- Python expert"
            }
        }

        response = status_handler_module.lambda_handler(sample_status_event, mock_lambda_context)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])

        assert body["job_id"] == "test-job-123"
        assert body["status"] == "completed"
        assert body["filename"] == "test-resume.pdf"
        assert "analysis_result" in body
        assert "Python expert" in body["analysis_result"]

    def test_get_processing_job(
        self, status_handler_module, sample_status_event, mock_lambda_context, mock_boto3_clients
    ) -> None:
        """Test retrieving a job that's still processing."""
        mock_boto3_clients["dynamodb_table"].get_item.return_value = {
            "Item": {
                "job_id": "test-job-123",
                "status": "processing",
                "filename": "test-resume.pdf",
                "created_at": "2025-11-22T17:00:00",
            }
        }

        response = status_handler_module.lambda_handler(sample_status_event, mock_lambda_context)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])

        assert body["status"] == "processing"
        assert "analysis_result" not in body

    def test_get_failed_job(
        self, status_handler_module, sample_status_event, mock_lambda_context, mock_boto3_clients
    ) -> None:
        """Test retrieving a failed job."""
        mock_boto3_clients["dynamodb_table"].get_item.return_value = {
            "Item": {
                "job_id": "test-job-123",
                "status": "failed",
                "filename": "test-resume.pdf",
                "created_at": "2025-11-22T17:00:00",
                "error": "PDF parsing failed",
            }
        }

        response = status_handler_module.lambda_handler(sample_status_event, mock_lambda_context)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])

        assert body["status"] == "failed"
        assert "error" in body
        assert body["error"] == "PDF parsing failed"
        assert "analysis_result" not in body

    def test_job_not_found(
        self, status_handler_module, sample_status_event, mock_lambda_context, mock_boto3_clients
    ) -> None:
        """Test retrieving a non-existent job."""
        mock_boto3_clients["dynamodb_table"].get_item.return_value = {}

        response = status_handler_module.lambda_handler(sample_status_event, mock_lambda_context)

        assert response["statusCode"] == 404
        body = json.loads(response["body"])
        assert "Job not found" in body["error"]

    def test_missing_job_id(self, status_handler_module, mock_lambda_context) -> None:
        """Test request without job_id parameter."""
        event = {"httpMethod": "GET", "path": "/status/", "pathParameters": {}}

        response = status_handler_module.lambda_handler(event, mock_lambda_context)

        assert response["statusCode"] == 400
        body = json.loads(response["body"])
        assert "Missing job_id" in body["error"]

    def test_missing_path_parameters(self, status_handler_module, mock_lambda_context) -> None:
        """Test request without pathParameters at all."""
        event = {"httpMethod": "GET", "path": "/status/test-job-123"}

        response = status_handler_module.lambda_handler(event, mock_lambda_context)

        assert response["statusCode"] == 400
        body = json.loads(response["body"])
        assert "Missing job_id" in body["error"]

    def test_cors_headers(self, status_handler_module, sample_status_event, mock_lambda_context) -> None:
        """Test that CORS headers are present in all responses."""
        response = status_handler_module.lambda_handler(sample_status_event, mock_lambda_context)

        assert "Access-Control-Allow-Origin" in response["headers"]
        assert response["headers"]["Access-Control-Allow-Origin"] == "*"

    def test_dynamodb_error(
        self, status_handler_module, sample_status_event, mock_lambda_context, mock_boto3_clients
    ) -> None:
        """Test error handling when DynamoDB fails."""
        mock_boto3_clients["dynamodb_table"].get_item.side_effect = Exception("DynamoDB error")

        response = status_handler_module.lambda_handler(sample_status_event, mock_lambda_context)

        assert response["statusCode"] == 500
        body = json.loads(response["body"])
        assert "error" in body
        assert "type" in body

    def test_job_with_unknown_status(
        self, status_handler_module, sample_status_event, mock_lambda_context, mock_boto3_clients
    ) -> None:
        """Test handling of job with unknown status."""
        mock_boto3_clients["dynamodb_table"].get_item.return_value = {
            "Item": {"job_id": "test-job-123", "filename": "test-resume.pdf"}
        }

        response = status_handler_module.lambda_handler(sample_status_event, mock_lambda_context)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["status"] == "unknown"

    def test_response_structure(
        self, status_handler_module, sample_status_event, mock_lambda_context, mock_boto3_clients
    ) -> None:
        """Test that response always has required fields."""
        mock_boto3_clients["dynamodb_table"].get_item.return_value = {
            "Item": {
                "job_id": "test-job-123",
                "status": "processing",
                "filename": "test.pdf",
                "created_at": "2025-11-22T17:00:00",
            }
        }

        response = status_handler_module.lambda_handler(sample_status_event, mock_lambda_context)

        assert response["statusCode"] == 200
        assert "headers" in response
        assert "body" in response
        assert response["headers"]["Content-Type"] == "application/json"

        body = json.loads(response["body"])
        assert "job_id" in body
        assert "status" in body

    def test_optional_fields_handling(
        self, status_handler_module, sample_status_event, mock_lambda_context, mock_boto3_clients
    ) -> None:
        """Test handling when optional fields are missing."""
        mock_boto3_clients["dynamodb_table"].get_item.return_value = {
            "Item": {"job_id": "test-job-123", "status": "completed"}
        }

        response = status_handler_module.lambda_handler(sample_status_event, mock_lambda_context)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])


        assert body["filename"] is None
        assert body["created_at"] is None
