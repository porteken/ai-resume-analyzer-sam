import json
import logging
import os
from typing import Any

import boto3

logger = logging.getLogger(__name__)

dynamodb: Any = boto3.resource("dynamodb")
s3_client = boto3.client("s3")

RESULTS_TABLE = os.environ.get("RESULTS_TABLE")
RESUME_BUCKET = os.environ.get("RESUME_BUCKET")
ACCOUNT_ID = os.environ.get("AWS_ACCOUNT_ID", "")


_cors_raw = os.environ.get("CORS_ALLOWED_ORIGINS", "")
CORS_ALLOWED_ORIGINS: set[str] = {
    o.strip().rstrip("/") for o in _cors_raw.split(",") if o.strip()
}

_results_table: Any | None = None


def get_results_table() -> Any:
    """Lazily initialise and return the DynamoDB results table."""
    global _results_table  # noqa: PLW0603
    if _results_table is None:
        if not RESULTS_TABLE:
            raise RuntimeError("RESULTS_TABLE environment variable not configured")
        _results_table = dynamodb.Table(RESULTS_TABLE)
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
    for field in ("strengths", "gaps", "recommendations"):
        normalized[field] = coerce_string_list(normalized.get(field))
    return normalized
