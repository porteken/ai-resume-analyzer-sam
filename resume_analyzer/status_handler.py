import json
import logging
import os
from typing import Any

import boto3

dynamodb: Any = boto3.resource("dynamodb")
RESULTS_TABLE = os.environ.get("RESULTS_TABLE")
CORS_ALLOW_ORIGIN = os.environ.get("CORS_ALLOW_ORIGIN", "*")
results_table: Any | None = dynamodb.Table(RESULTS_TABLE) if RESULTS_TABLE else None

logger = logging.getLogger(__name__)


def _response(status_code: int, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": CORS_ALLOW_ORIGIN,
        },
        "body": json.dumps(payload),
    }


def _validate_results_table() -> None:
    """Validate that results_table is configured."""
    if not results_table:
        raise RuntimeError("RESULTS_TABLE environment variable not configured")


def _coerce_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []

    result: list[str] = []
    for item in value:
        if isinstance(item, str):
            text = item.strip()
            if text:
                result.append(text)
    return result


def _normalize_analysis_result(value: Any) -> Any:
    if not isinstance(value, dict):
        return value

    normalized = dict(value)
    for field in ("strengths", "gaps", "recommendations"):
        normalized[field] = _coerce_string_list(normalized.get(field))

    return normalized


def _get_job_item(job_id: str) -> dict[str, Any] | None:
    """Get job item from DynamoDB."""
    if not results_table:
        return None
    response = results_table.get_item(Key={"job_id": job_id})
    return response.get("Item")


def lambda_handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Status handler that returns the analysis result for a given job ID."""
    logger.info("Status handler invoked")

    try:
        job_id = event.get("pathParameters", {}).get("job_id")

        if not job_id:
            return _response(400, {"error": "Missing job_id in path"})

        _validate_results_table()

        item = _get_job_item(job_id)
        if not item:
            return _response(404, {"error": "Job not found"})

        result = {
            "job_id": job_id,
            "status": item.get("status", "unknown"),
            "filename": item.get("filename"),
            "created_at": item.get("created_at"),
        }

        if item.get("status") == "completed":
            result["analysis_result"] = _normalize_analysis_result(
                item.get("analysis_result")
            )
        elif item.get("status") == "failed":
            result["error"] = item.get("error")

        return _response(200, result)
    except Exception as exc:
        logger.exception("Status handler failed")
        return _response(500, {"error": str(exc), "type": type(exc).__name__})
