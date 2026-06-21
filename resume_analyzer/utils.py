"""Shared AWS client, response, and normalization helpers."""

import json
import logging
import os
from typing import TYPE_CHECKING, Any

import boto3
from botocore.config import Config

if TYPE_CHECKING:
    from botocore.config import _RetryDict

logger = logging.getLogger(__name__)

RESULTS_TABLE = os.environ.get("RESULTS_TABLE")
RESUME_BUCKET = os.environ.get("RESUME_BUCKET")
ACCOUNT_ID = os.environ.get("AWS_ACCOUNT_ID", "")


_cors_raw = os.environ.get("CORS_ALLOWED_ORIGINS", "")
CORS_ALLOWED_ORIGINS: set[str] = {o.strip().rstrip("/") for o in _cors_raw.split(",") if o.strip()}

_CLIENT_CACHE: dict[str, Any | None] = {
    "dynamodb": None,
    "s3": None,
    "lambda": None,
    "results_table": None,
}


_BOTO_RETRIES: "_RetryDict" = {"max_attempts": 3, "mode": "standard"}

_BOTO_CLIENT_CONFIG = Config(
    tcp_keepalive=True,
    retries=_BOTO_RETRIES,
)


def get_dynamodb_resource() -> Any:
    """Lazily initialize and return the DynamoDB resource."""
    if _CLIENT_CACHE["dynamodb"] is None:
        _CLIENT_CACHE["dynamodb"] = boto3.resource("dynamodb", config=_BOTO_CLIENT_CONFIG)
    return _CLIENT_CACHE["dynamodb"]


def get_s3_client() -> Any:
    """Lazily initialize and return the S3 client."""
    if _CLIENT_CACHE["s3"] is None:
        _CLIENT_CACHE["s3"] = boto3.client("s3", config=_BOTO_CLIENT_CONFIG)
    return _CLIENT_CACHE["s3"]


def get_lambda_client() -> Any:
    """Lazily initialize and return the Lambda client."""
    if _CLIENT_CACHE["lambda"] is None:
        _CLIENT_CACHE["lambda"] = boto3.client("lambda", config=_BOTO_CLIENT_CONFIG)
    return _CLIENT_CACHE["lambda"]


def reset_cached_clients() -> None:
    """Reset cached boto3 clients and table references used by tests."""
    _CLIENT_CACHE.update(
        {
            "dynamodb": None,
            "s3": None,
            "lambda": None,
            "results_table": None,
        }
    )


def get_results_table() -> Any:
    """Lazily initialize and return the DynamoDB results table."""
    if _CLIENT_CACHE["results_table"] is None:
        if not RESULTS_TABLE:
            raise RuntimeError("RESULTS_TABLE environment variable not configured")
        _CLIENT_CACHE["results_table"] = get_dynamodb_resource().Table(RESULTS_TABLE)
    return _CLIENT_CACHE["results_table"]


def reset_results_table() -> None:
    """Reset cached table reference (used by tests)."""
    _CLIENT_CACHE["results_table"] = None


def get_cors_origin(event: dict[str, Any] | None = None) -> str:
    """Return the matching CORS origin for the request, or empty string."""
    if not CORS_ALLOWED_ORIGINS:
        return ""
    if event:
        headers = event.get("headers") or {}
        origin = (headers.get("Origin") or headers.get("origin") or "").rstrip("/")
        if origin in CORS_ALLOWED_ORIGINS:
            return origin
    return ""


def api_response(
    status_code: int,
    payload: dict[str, Any],
    *,
    event: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build an API Gateway proxy response with CORS headers."""
    resp_headers: dict[str, str] = {"Content-Type": "application/json"}
    origin = get_cors_origin(event)
    if origin:
        resp_headers["Access-Control-Allow-Origin"] = origin
        resp_headers["Vary"] = "Origin"
    return {
        "statusCode": status_code,
        "headers": resp_headers,
        "body": json.dumps(payload),
    }


def coerce_string_list(value: Any) -> list[str]:
    """Coerce a value into a clean list of non-empty strings."""
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        if isinstance(item, str):
            text = item.strip()
            if text:
                result.append(text)
    return result


def coerce_object_list(value: Any) -> list[dict[str, Any]]:
    """Coerce a value into a clean list of dictionaries."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def normalize_analysis_result(analysis: Any, *, strict: bool = False) -> dict[str, Any]:
    """Normalize an analysis dict, back-filling list fields.

    When *strict* is True (Gemini output), it raises TypeError on non-dict input.
    When False (DynamoDB retrieval), returns the value unchanged.
    """
    if not isinstance(analysis, dict):
        if strict:
            raise TypeError("Gemini returned invalid JSON object")
        return analysis

    normalized = dict(analysis)
    normalized["skills"] = coerce_string_list(normalized.get("skills"))
    for field in ("experience", "education"):
        normalized[field] = coerce_object_list(normalized.get(field))
    for field in ("strengths", "gaps", "recommendations"):
        normalized[field] = coerce_string_list(normalized.get(field))
    return normalized
