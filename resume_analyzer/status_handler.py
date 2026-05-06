import logging
import time
from typing import Any

try:
    from resume_analyzer.utils import (
        api_response,
        get_results_table,
        normalize_analysis_result,
    )
except ImportError:
    from utils import (
        api_response,
        get_results_table,
        normalize_analysis_result,
    )

logger = logging.getLogger(__name__)


def _get_job_item(job_id: str) -> dict[str, Any] | None:
    """Get job item from DynamoDB."""
    table = get_results_table()
    response = table.get_item(Key={"job_id": job_id})
    return response.get("Item")


def _is_job_expired(ttl_value: Any) -> bool:
    if ttl_value in (None, ""):
        return False

    try:
        return int(ttl_value) <= int(time.time())
    except (TypeError, ValueError):
        return False


def lambda_handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Status handler that returns the analysis result for a given job ID."""
    logger.info("Status handler invoked")

    try:
        job_id = event.get("pathParameters", {}).get("job_id")

        if not job_id:
            return api_response(400, {"error": "Missing job_id in path"}, event=event)

        item = _get_job_item(job_id)
        if not item:
            return api_response(404, {"error": "Job not found"}, event=event)
        if _is_job_expired(item.get("ttl")):
            return api_response(404, {"error": "Job has expired"}, event=event)

        result: dict[str, Any] = {
            "job_id": job_id,
            "status": item.get("status", "unknown"),
            "filename": item.get("filename"),
            "created_at": item.get("created_at"),
        }

        if item.get("status") == "completed":
            result["analysis_result"] = normalize_analysis_result(item.get("analysis_result"))
        elif item.get("status") == "failed":
            result["error"] = item.get("error")

        return api_response(200, result, event=event)
    except Exception:
        logger.exception("Status handler failed")
        return api_response(500, {"error": "Internal server error"}, event=event)
