"""Pytest configuration and shared fixtures."""

import base64
import json
from typing import Any
from unittest.mock import MagicMock, Mock

import pytest


@pytest.fixture(autouse=True)
def mock_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set up environment variables for all tests."""
    monkeypatch.setenv("GOOGLE_API_KEY", "test-api-key-123")
    monkeypatch.setenv("RESUME_BUCKET", "test-resume-bucket")
    monkeypatch.setenv("RESULTS_TABLE", "test-results-table")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


@pytest.fixture
def mock_boto3_clients() -> dict[str, Any]:
    """Mock all boto3 clients and resources."""
    mock_s3_client = MagicMock()
    mock_dynamodb = MagicMock()
    mock_sts_client = MagicMock()


    mock_sts_client.get_caller_identity.return_value = {"Account": "123456789012"}


    mock_s3_client.get_object.return_value = {
        "Body": Mock(read=lambda: b"mock pdf content"),
        "Metadata": {"job_id": "test-job-123", "job_description": "Test description"},
    }
    mock_s3_client.head_object.return_value = {
        "Metadata": {"job_id": "test-job-123", "job_description": "Test description"}
    }
    mock_s3_client.put_object.return_value = {"ETag": "mock-etag"}
    mock_s3_client.exceptions = Mock()
    mock_s3_client.exceptions.NoSuchKey = type("NoSuchKey", (Exception,), {})


    mock_table = MagicMock()
    mock_table.put_item.return_value = {}
    mock_table.get_item.return_value = {
        "Item": {
            "job_id": "test-job-123",
            "status": "completed",
            "filename": "test-resume.pdf",
            "created_at": "2025-11-22T17:00:00",
            "analysis_result": "Mock analysis result",
        }
    }
    mock_table.update_item.return_value = {}
    mock_dynamodb.Table.return_value = mock_table

    return {
        "s3": mock_s3_client,
        "dynamodb": mock_dynamodb,
        "sts": mock_sts_client,
        "dynamodb_table": mock_table,
    }


@pytest.fixture
def sample_pdf_base64() -> str:
    """Sample PDF file encoded in base64."""
    pdf_content = b"""%PDF-1.4
1 0 obj
<<
/Type /Catalog
/Pages 2 0 R
>>
endobj
2 0 obj
<<
/Type /Pages
/Kids [3 0 R]
/Count 1
>>
endobj
3 0 obj
<<
/Type /Page
/Parent 2 0 R
/Resources <<
/Font <<
/F1 4 0 R
>>
>>
/MediaBox [0 0 612 792]
/Contents 5 0 R
>>
endobj
4 0 obj
<<
/Type /Font
/Subtype /Type1
/BaseFont /Helvetica
>>
endobj
5 0 obj
<<
/Length 44
>>
stream
BT
/F1 12 Tf
100 700 Td
(Sample Resume) Tj
ET
endstream
endobj
xref
0 6
0000000000 65535 f
0000000009 00000 n
0000000058 00000 n
0000000115 00000 n
0000000262 00000 n
0000000341 00000 n
trailer
<<
/Size 6
/Root 1 0 R
>>
startxref
434
%%EOF"""
    return base64.b64encode(pdf_content).decode("utf-8")


@pytest.fixture
def sample_upload_event(sample_pdf_base64: str) -> dict[str, Any]:
    """Sample API Gateway event for upload endpoint."""
    body_dict = {
        "pdf_base64": sample_pdf_base64,
        "filename": "test-resume.pdf",
        "job_description": "Python developer position",
    }
    return {
        "httpMethod": "POST",
        "path": "/upload",
        "headers": {"Content-Type": "application/json"},
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
        "headers": {"Content-Type": "application/json"},
    }


@pytest.fixture
def sample_s3_event() -> dict[str, Any]:
    """Sample S3 event for Lambda trigger."""
    return {
        "Records": [
            {
                "eventVersion": "2.1",
                "eventSource": "aws:s3",
                "awsRegion": "us-east-1",
                "eventTime": "2025-11-22T17:00:00.000Z",
                "eventName": "ObjectCreated:Put",
                "s3": {
                    "bucket": {
                        "name": "test-resume-bucket",
                        "arn": "arn:aws:s3:::test-resume-bucket",
                    },
                    "object": {
                        "key": "uploads/20251122_170000_test-job-123_resume.pdf",
                        "size": 1024,
                    },
                },
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
    context.log_stream_name = "2025/11/22/[$LATEST]abcd1234"
    return context
