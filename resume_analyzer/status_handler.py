import json
import logging
import os
from typing import Any

import boto3

dynamodb = boto3.resource("dynamodb")
RESULTS_TABLE = os.environ.get("RESULTS_TABLE")

logger = logging.getLogger(__name__)


def lambda_handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Status handler that returns the analysis result for a given job ID."""
    logger.info("Status handler invoked: %s", json.dumps(event, default=str)[:500])

    try:
        job_id = event.get("pathParameters", {}).get("job_id")

        if not job_id:
            return {
                "statusCode": 400,
                "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
                "body": json.dumps({"error": "Missing job_id in path"}),
            }

        table = dynamodb.Table(RESULTS_TABLE)  # type: ignore[attr-defined]
        response = table.get_item(Key={"job_id": job_id})

        if "Item" not in response:
            return {
                "statusCode": 404,
                "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
                "body": json.dumps({"error": "Job not found"}),
            }

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

        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
            "body": json.dumps(result),
        }

    except Exception as e:
        logger.exception("ERROR")
        return {
            "statusCode": 500,
            "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
            "body": json.dumps({"error": str(e), "type": type(e).__name__}),
        }
