"""Unit tests for status_handler.py."""

import time
from typing import Any

import pytest
from botocore.exceptions import ClientError

from tests.test_utils import assert_error_response, assert_success_response


@pytest.fixture
def status_handler_module(mock_boto3_clients) -> Any:
    """Import status_handler with mocked dependencies."""
    from resume_analyzer import status_handler

    original_get_results_table = status_handler.get_results_table
    status_handler.get_results_table = lambda: mock_boto3_clients["dynamodb_table"]

    try:
        yield status_handler
    finally:
        status_handler.get_results_table = original_get_results_table


@pytest.mark.integration
@pytest.mark.aws
class TestStatusHandler:
    """Test suite for the status_handler lambda function."""

    def test_get_completed_job(
        self,
        status_handler_module,
        sample_status_event,
        mock_lambda_context,
        mock_boto3_clients,
    ) -> None:
        """Test retrieving a completed job."""
        mock_boto3_clients["dynamodb_table"].get_item.return_value = {
            "Item": {
                "job_id": "test-job-123",
                "status": "completed",
                "filename": "test-resume.pdf",
                "created_at": "2025-11-22T17:00:00",
                "analysis_result": {
                    "name": "Jane Doe",
                    "contact_info": {
                        "email": "jane@example.com",
                        "phone": "555-0100",
                        "location": "Austin, TX",
                        "linkedin": "https://linkedin.com/in/janedoe",
                    },
                    "summary": "Strong profile",
                    "skills": ["Python"],
                    "experience": [],
                    "education": [],
                    "strengths": ["Python backend expertise"],
                    "gaps": [],
                    "recommendations": [],
                },
            }
        }

        response = status_handler_module.lambda_handler(sample_status_event, mock_lambda_context)

        body = assert_success_response(
            response,
            200,
            required_fields=["job_id", "status", "filename", "analysis_result"],
        )

        assert body["job_id"] == "test-job-123"
        assert body["status"] == "completed"
        assert body["filename"] == "test-resume.pdf"
        assert body["analysis_result"]["name"] == "Jane Doe"
        assert body["analysis_result"]["strengths"] == ["Python backend expertise"]

    def test_get_completed_job_backfills_missing_analysis_lists(
        self,
        status_handler_module,
        sample_status_event,
        mock_lambda_context,
        mock_boto3_clients,
    ) -> None:
        mock_boto3_clients["dynamodb_table"].get_item.return_value = {
            "Item": {
                "job_id": "test-job-123",
                "status": "completed",
                "analysis_result": {
                    "name": "Jane Doe",
                    "summary": "Strong profile",
                },
            }
        }

        response = status_handler_module.lambda_handler(sample_status_event, mock_lambda_context)
        body = assert_success_response(response, 200, required_fields=["analysis_result"])

        assert body["analysis_result"]["strengths"] == []
        assert body["analysis_result"]["gaps"] == []
        assert body["analysis_result"]["recommendations"] == []

    def test_get_processing_job(
        self,
        status_handler_module,
        sample_status_event,
        mock_lambda_context,
        mock_boto3_clients,
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

        body = assert_success_response(response, 200, required_fields=["job_id", "status"])

        assert body["status"] == "processing"
        assert "analysis_result" not in body

    def test_get_failed_job(
        self,
        status_handler_module,
        sample_status_event,
        mock_lambda_context,
        mock_boto3_clients,
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

        body = assert_success_response(response, 200, required_fields=["job_id", "status", "error"])

        assert body["status"] == "failed"
        assert body["error"] == "PDF parsing failed"
        assert "analysis_result" not in body

    def test_job_not_found(
        self,
        status_handler_module,
        sample_status_event,
        mock_lambda_context,
        mock_boto3_clients,
    ) -> None:
        """Test retrieving a non-existent job."""
        mock_boto3_clients["dynamodb_table"].get_item.return_value = {}

        response = status_handler_module.lambda_handler(sample_status_event, mock_lambda_context)
        assert_error_response(response, 404, "Job not found")

    def test_expired_job_returns_not_found(
        self,
        status_handler_module,
        sample_status_event,
        mock_lambda_context,
        mock_boto3_clients,
    ) -> None:
        mock_boto3_clients["dynamodb_table"].get_item.return_value = {
            "Item": {
                "job_id": "test-job-123",
                "status": "completed",
                "ttl": int(time.time()) - 60,
            }
        }

        response = status_handler_module.lambda_handler(sample_status_event, mock_lambda_context)

        assert_error_response(response, 404, "expired")

    @pytest.mark.parametrize(
        ("event", "expected_error"),
        [
            (
                {"httpMethod": "GET", "path": "/status/", "pathParameters": {}},
                "Missing job_id",
            ),
            ({"httpMethod": "GET", "path": "/status/test-job-123"}, "Missing job_id"),
        ],
    )
    def test_missing_job_id_scenarios(
        self, status_handler_module, mock_lambda_context, event, expected_error
    ) -> None:
        """Test request without a job_id parameter in various scenarios."""
        response = status_handler_module.lambda_handler(event, mock_lambda_context)
        assert_error_response(response, 400, expected_error)

    def test_dynamodb_error(
        self,
        status_handler_module,
        sample_status_event,
        mock_lambda_context,
        mock_boto3_clients,
    ) -> None:
        """Test error handling when DynamoDB fails."""
        mock_boto3_clients["dynamodb_table"].get_item.side_effect = ClientError(
            {"Error": {"Code": "InternalServerError"}},
            "GetItem",
        )

        response = status_handler_module.lambda_handler(sample_status_event, mock_lambda_context)

        assert_error_response(response, 500)

    def test_job_with_unknown_status(
        self,
        status_handler_module,
        sample_status_event,
        mock_lambda_context,
        mock_boto3_clients,
    ) -> None:
        """Test handling of a job with unknown status."""
        mock_boto3_clients["dynamodb_table"].get_item.return_value = {
            "Item": {"job_id": "test-job-123", "filename": "test-resume.pdf"}
        }

        response = status_handler_module.lambda_handler(sample_status_event, mock_lambda_context)

        body = assert_success_response(response, 200, required_fields=["job_id", "status"])
        assert body["status"] == "unknown"

    def test_optional_fields_handling(
        self,
        status_handler_module,
        sample_status_event,
        mock_lambda_context,
        mock_boto3_clients,
    ) -> None:
        """Test handling when optional fields are missing."""
        mock_boto3_clients["dynamodb_table"].get_item.return_value = {
            "Item": {"job_id": "test-job-123", "status": "completed"}
        }

        response = status_handler_module.lambda_handler(sample_status_event, mock_lambda_context)

        body = assert_success_response(response, 200, required_fields=["job_id", "status"])

        assert body["filename"] is None
        assert body["created_at"] is None

    def test_results_table_not_configured(self, status_handler_module, mock_lambda_context) -> None:
        """Test RuntimeError when RESULTS_TABLE is not set."""
        original = status_handler_module.get_results_table

        def _no_table() -> None:
            raise RuntimeError("RESULTS_TABLE environment variable not configured")

        status_handler_module.get_results_table = _no_table
        try:
            event = {
                "pathParameters": {"job_id": "test-job-123"},
                "headers": {"Origin": "https://test.example.com"},
            }
            response = status_handler_module.lambda_handler(event, mock_lambda_context)
            assert_error_response(response, 500)
        finally:
            status_handler_module.get_results_table = original
