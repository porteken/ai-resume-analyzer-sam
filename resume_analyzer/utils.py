import json
import logging
import os
from typing import Any

import boto3
from botocore.config import Config

logger = logging.getLogger(__name__)

RESULTS_TABLE = os.environ.get("RESULTS_TABLE")
RESUME_BUCKET = os.environ.get("RESUME_BUCKET")
ACCOUNT_ID = os.environ.get("AWS_ACCOUNT_ID", "")


_cors_raw = os.environ.get("CORS_ALLOWED_ORIGINS", "")
CORS_ALLOWED_ORIGINS: set[str] = {o.strip().rstrip("/") for o in _cors_raw.split(",") if o.strip()}

dynamodb: Any | None = None
s3_client: Any | None = None
_results_table: Any | None = None

_BOTO_CLIENT_CONFIG = Config(
    tcp_keepalive=True,
    retries={"max_attempts": 3, "mode": "standard"},
)


def get_dynamodb_resource() -> Any:
    """Lazily initialise and return the DynamoDB resource."""
    global dynamodb  # noqa: PLW0603
    if dynamodb is None:
        dynamodb = boto3.resource("dynamodb", config=_BOTO_CLIENT_CONFIG)
    return dynamodb


def get_s3_client() -> Any:
    """Lazily initialise and return the S3 client."""
    global s3_client  # noqa: PLW0603
    if s3_client is None:
        s3_client = boto3.client("s3", config=_BOTO_CLIENT_CONFIG)
    return s3_client


def reset_cached_clients() -> None:
    """Reset cached boto3 clients and table references used by tests."""
    global dynamodb, s3_client, _results_table  # noqa: PLW0603
    dynamodb = None
    s3_client = None
    _results_table = None


def get_results_table() -> Any:
    """Lazily initialise and return the DynamoDB results table."""
    global _results_table  # noqa: PLW0603
    if _results_table is None:
        if not RESULTS_TABLE:
            raise RuntimeError("RESULTS_TABLE environment variable not configured")
        _results_table = get_dynamodb_resource().Table(RESULTS_TABLE)
    return _results_table


def reset_results_table() -> None:
    """Reset cached table reference (used by tests)."""
    global _results_table  # noqa: PLW0603
    _results_table = None


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
    """Normalise an analysis dict, back-filling list fields.

    When *strict* is True (Gemini output), raises TypeError on non-dict input.
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
