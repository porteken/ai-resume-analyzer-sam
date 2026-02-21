import json
import logging
import os
from typing import Any

import boto3

dynamodb = boto3.resource("dynamodb")
RESULTS_TABLE = os.environ.get("RESULTS_TABLE")
CORS_ALLOW_ORIGIN = os.environ.get("CORS_ALLOW_ORIGIN", "https://app.example.com")
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


def lambda_handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Status handler that returns the analysis result for a given job ID."""
    logger.info("Status handler invoked")

    try:
        job_id = event.get("pathParameters", {}).get("job_id")

        if not job_id:
            return _response(400, {"error": "Missing job_id in path"})

        if not results_table:
            raise RuntimeError("RESULTS_TABLE environment variable not configured")

        response = results_table.get_item(Key={"job_id": job_id})
        if "Item" not in response:
            return _response(404, {"error": "Job not found"})

        item = response["Item"]
        result = {
            "job_id": job_id,
            "status": item.get("status", "unknown"),
            "filename": item.get("filename"),
            "created_at": item.get("created_at"),
        }

        if item.get("status") == "completed":
            result["analysis_result"] = item.get("analysis_result")
        elif item.get("status") == "failed":
            result["error"] = item.get("error")

        return _response(200, result)
    except Exception as exc:
        logger.exception("Status handler failed")
        return _response(500, {"error": str(exc), "type": type(exc).__name__})
