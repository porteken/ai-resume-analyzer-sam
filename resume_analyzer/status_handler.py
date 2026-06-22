"""Return analysis job status from DynamoDB."""

import importlib
import logging
import time
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

try:
    _utils = importlib.import_module("resume_analyzer.utils")
except ImportError:
    _utils = importlib.import_module("utils")

api_response = _utils.api_response
get_results_table = _utils.get_results_table
normalize_analysis_result = _utils.normalize_analysis_result

logger = logging.getLogger(__name__)


def _get_job_item(job_id: str) -> dict[str, Any] | None:
    """Get job item from DynamoDB."""
    table = get_results_table()
    response = table.get_item(Key={"job_id": job_id})
    item = response.get("Item")
    return item if isinstance(item, dict) else None


def _is_job_expired(ttl_value: Any) -> bool:
    if ttl_value in (None, ""):
        return False

    try:
        return int(ttl_value) <= int(time.time())
    except (TypeError, ValueError):
        return False


def _error_response(event: dict[str, Any], status_code: int, message: str) -> dict[str, Any]:
    return api_response(status_code, {"error": message}, event=event)


def _job_id_from_event(event: dict[str, Any]) -> str | None:
    path_params = event.get("pathParameters", {})
    if isinstance(path_params, dict):
        job_id = path_params.get("job_id")
        return str(job_id) if job_id else None
    return None


def _status_payload(job_id: str, item: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "job_id": job_id,
        "status": item.get("status", "unknown"),
        "filename": item.get("filename"),
        "created_at": item.get("created_at"),
    }
    _append_terminal_status(result, item)
    return result


def _append_terminal_status(result: dict[str, Any], item: dict[str, Any]) -> None:
    status = item.get("status")
    if status == "completed":
        result["analysis_result"] = normalize_analysis_result(item.get("analysis_result"))
    elif status == "failed":
        result["error"] = item.get("error")


def lambda_handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Status handler that returns the analysis result for a given job ID."""
    logger.info("Status handler invoked")

    try:
        job_id = _job_id_from_event(event)

        if not job_id:
            return _error_response(event, 400, "Missing job_id in path")

        item = _get_job_item(job_id)
        if not item:
            return _error_response(event, 404, "Job not found")
        if _is_job_expired(item.get("ttl")):
            return _error_response(event, 404, "Job has expired")

        return api_response(200, _status_payload(job_id, item), event=event)
    except (BotoCoreError, ClientError, RuntimeError):
        logger.exception("Status handler failed")
        return _error_response(event, 500, "Internal server error")
    except (AttributeError, TypeError, ValueError):
        logger.exception("Unexpected status handler failure")
        return _error_response(event, 500, "Internal server error")
