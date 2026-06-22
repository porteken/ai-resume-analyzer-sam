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

RESULTS_TABLE = os.getenv("RESULTS_TABLE")
RESUME_BUCKET = os.getenv("RESUME_BUCKET")
ACCOUNT_ID = os.getenv("AWS_ACCOUNT_ID", "")


def _origin_set(raw_origins: str) -> set[str]:
    return {origin.strip().rstrip("/") for origin in raw_origins.split(",") if origin.strip()}


CORS_ALLOWED_ORIGINS = _origin_set(os.getenv("CORS_ALLOWED_ORIGINS", ""))

CLIENT_CACHE_KEYS = ("dynamodb", "s3", "lambda", "results_table")
_CLIENT_CACHE: dict[str, Any | None] = dict.fromkeys(CLIENT_CACHE_KEYS)


_BOTO_RETRIES: "_RetryDict" = {"max_attempts": 3, "mode": "standard"}

_BOTO_CLIENT_CONFIG = Config(
    tcp_keepalive=True,
    retries=_BOTO_RETRIES,
)


def get_dynamodb_resource() -> Any:
    """Lazily initialize and return the DynamoDB resource."""
    return _cached_aws("dynamodb", boto3.resource, "dynamodb")


def get_s3_client() -> Any:
    """Lazily initialize and return the S3 client."""
    return _cached_aws("s3", boto3.client, "s3")


def get_lambda_client() -> Any:
    """Lazily initialize and return the Lambda client."""
    return _cached_aws("lambda", boto3.client, "lambda")


def _cached_aws(cache_key: str, factory: Any, service_name: str) -> Any:
    if _CLIENT_CACHE[cache_key] is None:
        _CLIENT_CACHE[cache_key] = factory(service_name, config=_BOTO_CLIENT_CONFIG)
    return _CLIENT_CACHE[cache_key]


def reset_cached_clients() -> None:
    """Reset cached boto3 clients and table references used by tests."""
    _CLIENT_CACHE.update(dict.fromkeys(CLIENT_CACHE_KEYS))


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
    if not CORS_ALLOWED_ORIGINS or not event:
        return ""
    origin = _request_origin(event)
    return origin if origin in CORS_ALLOWED_ORIGINS else ""


def _request_origin(event: dict[str, Any]) -> str:
    headers = event.get("headers") or {}
    return (headers.get("Origin") or headers.get("origin") or "").rstrip("/")


def api_response(
    status_code: int,
    payload: dict[str, Any],
    *,
    event: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build an API Gateway proxy response with CORS headers."""
    return {
        "statusCode": status_code,
        "headers": _response_headers(event),
        "body": json.dumps(payload),
    }


def _response_headers(event: dict[str, Any] | None) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    origin = get_cors_origin(event)
    if origin:
        headers.update({"Access-Control-Allow-Origin": origin, "Vary": "Origin"})
    return headers


def coerce_string_list(value: Any) -> list[str]:
    """Coerce a value into a clean list of non-empty strings."""
    if not isinstance(value, list):
        return []
    return [text for item in value if isinstance(item, str) if (text := item.strip())]


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
    _normalize_fields(
        normalized,
        ("skills", "strengths", "gaps", "recommendations"),
        coerce_string_list,
    )
    _normalize_fields(normalized, ("experience", "education"), coerce_object_list)
    return normalized


def _normalize_fields(
    target: dict[str, Any],
    fields: tuple[str, ...],
    normalizer: Any,
) -> None:
    for field in fields:
        target[field] = normalizer(target.get(field))
