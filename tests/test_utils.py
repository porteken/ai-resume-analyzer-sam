"""Shared test utilities and common assertion helpers."""

import json
import os
from typing import Any
from unittest.mock import MagicMock


def assert_cors_headers(response: dict[str, Any]) -> None:
    """Assert that a response contains proper CORS headers."""
    assert "headers" in response, "Response must contain headers"
    headers = response["headers"]

    assert "Access-Control-Allow-Origin" in headers, "Missing CORS origin header"
    expected_origin = os.environ.get("CORS_ALLOW_ORIGIN", "*")
    assert headers["Access-Control-Allow-Origin"] == expected_origin


def assert_response_structure(
    response: dict[str, Any],
    expected_status: int,
    should_have_body: bool = True,
) -> None:
    """Assert that a response has the correct API Gateway structure."""
    assert "statusCode" in response, "Response must contain statusCode"
    assert (
        response["statusCode"] == expected_status
    ), f"Expected status {expected_status}, got {response['statusCode']}"

    if should_have_body:
        assert "body" in response, "Response must contain body"
        try:
            json.loads(response["body"])
        except json.JSONDecodeError as err:
            raise AssertionError("Response body must be valid JSON") from err


def assert_error_response(
    response: dict[str, Any],
    expected_status: int,
    expected_error_message: str | None = None,
) -> dict[str, Any]:
    """Assert that a response is a properly formatted error response."""
    assert_response_structure(response, expected_status)

    if "headers" in response:
        assert_cors_headers(response)

    body = json.loads(response["body"])
    assert "error" in body, "Error response must contain 'error' field"

    if expected_error_message:
        assert (
            expected_error_message in body["error"]
        ), f"Expected error message to contain '{expected_error_message}', got '{body['error']}'"

    return body


def assert_success_response(
    response: dict[str, Any],
    expected_status: int = 200,
    required_fields: list | None = None,
) -> dict[str, Any]:
    """Assert that a response is a successful response with required fields."""
    assert_response_structure(response, expected_status)
    assert_cors_headers(response)

    body = json.loads(response["body"])

    if required_fields:
        for field in required_fields:
            assert field in body, f"Response body must contain '{field}' field"

    return body


def create_mock_event(
    body: dict[str, Any] | None = None,
    http_method: str = "POST",
    path: str = "/",
    query_params: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Create a mock API Gateway event for testing."""
    return {
        "httpMethod": http_method,
        "path": path,
        "queryStringParameters": query_params or {},
        "headers": headers or {},
        "body": json.dumps(body) if body else None,
        "isBase64Encoded": False,
        "requestContext": {
            "requestId": "test-request-id",
            "accountId": "123456789012",
            "stage": "test",
        },
    }


def create_mock_context(
    function_name: str = "test-function",
    request_id: str = "test-request-id",
    memory_limit: int = 128,
) -> MagicMock:
    """Create a mock Lambda context object."""
    context = MagicMock()
    context.function_name = function_name
    context.aws_request_id = request_id
    context.memory_limit_in_mb = memory_limit
    context.invoked_function_arn = (
        f"arn:aws:lambda:us-east-1:123456789012:function:{function_name}"
    )

    return context
