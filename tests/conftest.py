"""Pytest configuration and shared fixtures."""

import json
import sys
from types import ModuleType
from typing import Any
from unittest.mock import MagicMock, Mock

import pytest


class FakePart:
    @staticmethod
    def from_bytes(data: bytes, mime_type: str) -> dict[str, Any]:
        return {"data": data, "mime_type": mime_type}


class FakeGenerateContentConfig:
    def __init__(self, **kwargs: Any) -> None:
        self.__dict__.update(kwargs)


class FakeTypes:
    def __getattr__(self, name: str) -> Any:
        if name == "Part":
            return FakePart
        if name == "GenerateContentConfig":
            return FakeGenerateContentConfig
        raise AttributeError(f"'{type(self).__name__}' has no attribute '{name}'")


_fake_genai_module: Any = ModuleType("google.genai")
_fake_types_module: Any = ModuleType("google.genai.types")
_fake_types_module.Part = FakePart
_fake_types_module.GenerateContentConfig = FakeGenerateContentConfig
_fake_genai_module.types = _fake_types_module
_fake_genai_module.Client = MagicMock()
sys.modules["google.genai"] = _fake_genai_module

CONTENT_TYPE_JSON = "application/json"
TEST_ORIGIN = "https://test.example.com"


@pytest.fixture(autouse=True)
def mock_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set up environment variables for all tests."""
    monkeypatch.setenv("GOOGLE_API_KEY", "test-api-key-123")
    monkeypatch.setenv(
        "GOOGLE_API_KEY_SECRET_ARN",
        "arn:aws:secretsmanager:us-east-1:123456789012:secret:test",
    )
    monkeypatch.setenv("GEMINI_MODEL_ID", "gemini-3-flash-preview")
    monkeypatch.setenv("RESUME_BUCKET", "test-resume-bucket")
    monkeypatch.setenv("RESULTS_TABLE", "test-results-table")
    monkeypatch.setenv("AWS_ACCOUNT_ID", "123456789012")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "test-analyze-function")
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "https://test.example.com")

    from resume_analyzer import utils

    monkeypatch.setattr(utils, "CORS_ALLOWED_ORIGINS", {TEST_ORIGIN})
    utils.reset_cached_clients()


@pytest.fixture
def mock_boto3_clients() -> dict[str, Any]:
    """Mock all boto3 clients and resources."""
    mock_s3_client = MagicMock()
    mock_lambda_client = MagicMock()
    mock_dynamodb = MagicMock()

    mock_s3_client.get_object.return_value = {
        "Body": Mock(read=lambda: b"%PDF-1.4\nmock pdf bytes\n%%EOF"),
        "Metadata": {"job_id": "test-job-123", "job_description": "Test description"},
    }
    mock_s3_client.head_object.return_value = {
        "ContentLength": 128,
        "Metadata": {"job_id": "test-job-123", "job_description": "Test description"},
    }
    mock_s3_client.put_object.return_value = {"ETag": "mock-etag"}
    mock_s3_client.generate_presigned_post.return_value = {
        "url": "https://test-resume-bucket.s3.amazonaws.com",
        "fields": {
            "key": "uploads/test-job-123/test-resume.pdf",
            "Content-Type": "application/pdf",
            "x-amz-meta-job_id": "test-job-123",
        },
    }
    mock_lambda_client.invoke.return_value = {"StatusCode": 202}

    mock_table = MagicMock()
    mock_table.put_item.return_value = {}
    mock_table.get_item.return_value = {
        "Item": {
            "job_id": "test-job-123",
            "status": "completed",
            "filename": "test-resume.pdf",
            "created_at": "2026-02-21T17:00:00Z",
            "analysis_result": {
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
                "strengths": ["Strong Python and AWS delivery experience"],
                "gaps": ["No direct ML production ownership"],
                "recommendations": ["Add ML deployment examples"],
            },
        }
    }
    mock_table.update_item.return_value = {}
    mock_dynamodb.Table.return_value = mock_table

    return {
        "s3": mock_s3_client,
        "lambda": mock_lambda_client,
        "dynamodb": mock_dynamodb,
        "dynamodb_table": mock_table,
    }


@pytest.fixture
def sample_upload_event() -> dict[str, Any]:
    """Sample API Gateway event for upload endpoint."""
    body_dict = {
        "filename": "test-resume.pdf",
        "job_description": "Python developer position",
    }
    return {
        "httpMethod": "POST",
        "path": "/upload",
        "headers": {"Content-Type": CONTENT_TYPE_JSON, "Origin": TEST_ORIGIN},
        "body": json.dumps(body_dict),
        "isBase64Encoded": False,
    }


@pytest.fixture
def sample_analyze_event() -> dict[str, Any]:
    """Sample API Gateway event for analyze endpoint."""
    body_dict = {
        "job_id": "test-job-123",
        "s3_url": "s3://test-resume-bucket/uploads/test-job-123/test-resume.pdf",
        "job_description": "Python developer position",
    }
    return {
        "httpMethod": "POST",
        "path": "/analyze",
        "headers": {"Content-Type": CONTENT_TYPE_JSON, "Origin": TEST_ORIGIN},
        "body": json.dumps(body_dict),
        "isBase64Encoded": False,
    }


@pytest.fixture
def sample_status_event() -> dict[str, Any]:
    """Sample API Gateway event for status endpoint."""
    return {
        "httpMethod": "GET",
        "path": "/status/test-job-123",
        "pathParameters": {"job_id": "test-job-123"},
        "headers": {"Content-Type": CONTENT_TYPE_JSON, "Origin": TEST_ORIGIN},
    }


@pytest.fixture
def sample_s3_event() -> dict[str, Any]:
    """Sample S3 event shape used by generic unit tests."""
    return {
        "Records": [
            {
                "s3": {
                    "bucket": {"name": "test-resume-bucket"},
                    "object": {"key": "uploads/test-job-123/test-resume.pdf"},
                }
            }
        ]
    }


@pytest.fixture
def mock_lambda_context() -> MagicMock:
    """Mock Lambda context object."""
    context = MagicMock()
    context.function_name = "test-function"
    context.function_version = "$LATEST"
    context.invoked_function_arn = "arn:aws:lambda:us-east-1:123456789012:function:test-function"
    context.memory_limit_in_mb = 512
    context.aws_request_id = "test-request-id"
    context.log_group_name = "/aws/lambda/test-function"
    context.log_stream_name = "2026/02/21/[$LATEST]abcd1234"
    return context
