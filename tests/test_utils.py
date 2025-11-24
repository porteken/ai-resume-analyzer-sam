"""Shared test utilities and common assertion helpers."""

import json
from typing import Any
from unittest.mock import MagicMock


def assert_cors_headers(response: dict[str, Any]) -> None:
    """Assert that a response contains proper CORS headers.

    Args:
        response: The API Gateway response dictionary

    """
    assert "headers" in response, "Response must contain headers"
    headers = response["headers"]

    assert "Access-Control-Allow-Origin" in headers, "Missing CORS origin header"
    assert headers["Access-Control-Allow-Origin"] == "*", "CORS origin should be *"


def assert_response_structure(
    response: dict[str, Any],
    expected_status: int,
    should_have_body: bool = True,
) -> None:
    """Assert that a response has the correct API Gateway structure.

    Args:
        response: The API Gateway response dictionary
        expected_status: Expected HTTP status code
        should_have_body: Whether the response should have a body
        should_have_headers: Whether the response should have headers

    """
    assert "statusCode" in response, "Response must contain statusCode"
    assert response["statusCode"] == expected_status, \
        f"Expected status {expected_status}, got {response['statusCode']}"


    if should_have_body:
        assert "body" in response, "Response must contain body"
        try:
            json.loads(response["body"])
        except json.JSONDecodeError as err:
            raise AssertionError("Response body must be valid JSON") from err


def assert_error_response(
    response: dict[str, Any],
    expected_status: int,
    expected_error_message: str | None = None
) -> dict[str, Any]:
    """Assert that a response is a properly formatted error response.

    Args:
        response: The API Gateway response dictionary
        expected_status: Expected HTTP status code (should be 4xx or 5xx)
        expected_error_message: Optional expected error message substring

    Returns:
        The parsed response body as a dictionary

    """
    assert_response_structure(response, expected_status)

    if "headers" in response:
        assert_cors_headers(response)

    body = json.loads(response["body"])
    assert "error" in body, "Error response must contain 'error' field"

    if expected_error_message:
        assert expected_error_message in body["error"], \
            f"Expected error message to contain '{expected_error_message}', got '{body['error']}'"

    return body


def assert_success_response(
    response: dict[str, Any],
    expected_status: int = 200,
    required_fields: list | None = None
) -> dict[str, Any]:
    """Assert that a response is a successful response with required fields.

    Args:
        response: The API Gateway response dictionary
        expected_status: Expected HTTP status code (default: 200)
        required_fields: Optional list of required fields in response body

    Returns:
        The parsed response body as a dictionary

    """
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
    headers: dict[str, str] | None = None
) -> dict[str, Any]:
    """Create a mock API Gateway event for testing.

    Args:
        body: Request body as dictionary (will be JSON serialized)
        http_method: HTTP method (default: POST)
        path: Request path (default: /)
        query_params: Query string parameters
        headers: Request headers

    Returns:
        A mock API Gateway event dictionary

    """
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
            "stage": "test"
        }
    }


def create_mock_context(
    function_name: str = "test-function",
    request_id: str = "test-request-id",
    memory_limit: int = 128
) -> MagicMock:
    """Create a mock Lambda context object for testing.

    Args:
        function_name: Lambda function name
        request_id: Request ID
        memory_limit: Memory limit in MB

    Returns:
        A mock Lambda context object

    """
    context = MagicMock()
    context.function_name = function_name
    context.aws_request_id = request_id
    context.memory_limit_in_mb = memory_limit
    context.invoked_function_arn = f"arn:aws:lambda:us-east-1:123456789012:function:{function_name}"

    return context
