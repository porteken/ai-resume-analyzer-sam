import base64
import http
import json
import logging
import os
from datetime import UTC, datetime
from typing import Any
from urllib.parse import unquote, urlparse

from botocore.exceptions import ClientError
from google.genai import Client, types

try:
    from resume_analyzer.utils import (
        ACCOUNT_ID,
        RESUME_BUCKET,
        api_response,
        get_results_table,
        normalize_analysis_result,
        s3_client,
    )
except ImportError:
    from utils import (
        ACCOUNT_ID,
        RESUME_BUCKET,
        api_response,
        get_results_table,
        normalize_analysis_result,
        s3_client,
    )

logger = logging.getLogger(__name__)

GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")
GEMINI_MODEL_ID = os.environ.get("GEMINI_MODEL_ID", "gemini-2.5-flash")

RESUME_ANALYSIS_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "contact_info": {
            "type": "object",
            "properties": {
                "email": {"type": "string"},
                "phone": {"type": "string"},
                "location": {"type": "string"},
                "linkedin": {"type": "string"},
            },
            "required": ["email", "phone", "location", "linkedin"],
            "additionalProperties": False,
        },
        "summary": {"type": "string"},
        "skills": {
            "type": "array",
            "items": {"type": "string"},
        },
        "experience": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "company": {"type": "string"},
                    "role": {"type": "string"},
                    "duration": {"type": "string"},
                    "highlights": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": ["company", "role", "duration", "highlights"],
                "additionalProperties": False,
            },
        },
        "strengths": {
            "type": "array",
            "items": {"type": "string"},
        },
        "gaps": {
            "type": "array",
            "items": {"type": "string"},
        },
        "recommendations": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
    "required": [
        "name",
        "contact_info",
        "summary",
        "skills",
        "experience",
        "strengths",
        "gaps",
        "recommendations",
    ],
    "additionalProperties": False,
}


def _parse_s3_url(s3_url: str) -> tuple[str, str]:
    parsed = urlparse(s3_url)

    if parsed.scheme == "s3" and parsed.netloc:
        return parsed.netloc, unquote(parsed.path.lstrip("/"))

    if parsed.scheme in {"http", "https"} and parsed.netloc.endswith("amazonaws.com"):
        host_parts = parsed.netloc.split(".")
        path = parsed.path.lstrip("/")

        if host_parts[0] == "s3":
            parts = path.split("/", 1)
            expected_parts = 2
            if len(parts) != expected_parts:
                raise ValueError("Invalid S3 path-style URL")
            return parts[0], unquote(parts[1])

        min_host_parts = 3
        if len(host_parts) >= min_host_parts and host_parts[1] == "s3":
            return host_parts[0], unquote(path)

    raise ValueError("s3_url must be a valid s3:// or https://...amazonaws.com URL")


def _download_pdf_bytes(bucket: str, key: str) -> bytes:
    kwargs: dict[str, Any] = {"Bucket": bucket, "Key": key}
    if ACCOUNT_ID:
        kwargs["ExpectedBucketOwner"] = ACCOUNT_ID

    obj = s3_client.get_object(**kwargs)
    data = obj["Body"].read()

    if not data:
        raise ValueError("Downloaded PDF is empty")
    return data


def _get_genai_client() -> Any:
    return Client(api_key=GOOGLE_API_KEY)


def _get_genai_types() -> Any:
    return types


def analyze_resume_pdf(pdf_bytes: bytes, job_description: str) -> dict[str, Any]:
    """Calls Gemini 2.5 Flash with native PDF input and strict JSON schema output."""
    if not GOOGLE_API_KEY:
        raise RuntimeError("GOOGLE_API_KEY environment variable not configured")

    types = _get_genai_types()

    response_schema = RESUME_ANALYSIS_RESPONSE_SCHEMA
    prompt = (
        "Analyze this resume PDF against the provided job description and return JSON only. "
        "If a field is unknown, return an empty string or empty list. "
        "Provide concise, job-targeted strengths, gaps, and recommendations as string arrays.\n\n"
        f"Job Description:\n{job_description}"
    )

    response = _get_genai_client().models.generate_content(
        model=GEMINI_MODEL_ID,
        contents=[
            types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"),
            prompt,
        ],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_json_schema=response_schema,
            temperature=0.2,
        ),
    )

    text = getattr(response, "text", None)
    if not text:
        raise RuntimeError("Gemini returned an empty response")

    try:
        return normalize_analysis_result(json.loads(text), strict=True)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Gemini returned non-JSON output: {text[:200]}") from exc


def _update_job_status(
    job_id: str | None,
    status_value: str,
    analysis_result: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    if not job_id:
        return

    try:
        table = get_results_table()
    except RuntimeError:
        return

    expression = "SET #status = :status"
    names = {"#status": "status"}
    values: dict[str, Any] = {":status": status_value}

    if analysis_result is not None:
        expression += ", analysis_result = :result, completed_at = :completed"
        values[":result"] = analysis_result
        values[":completed"] = datetime.now(UTC).isoformat().replace("+00:00", "Z")

    if error:
        expression += ", #error = :error"
        names["#error"] = "error"
        values[":error"] = error

    try:
        table.update_item(
            Key={"job_id": job_id},
            UpdateExpression=expression,
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
    except Exception:
        logger.exception("Failed to update DynamoDB status for job_id=%s", job_id)


def _get_job_record(job_id: str) -> dict[str, Any]:
    try:
        table = get_results_table()
        item = table.get_item(Key={"job_id": job_id}).get("Item")
        return item if isinstance(item, dict) else {}
    except Exception:
        logger.exception("Failed to read DynamoDB record for job_id=%s", job_id)
        return {}


def _extract_request(event: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    if "body" not in event:
        return None, "No body in request"

    try:
        body: Any = event.get("body", "")
        if event.get("isBase64Encoded", False):
            body = base64.b64decode(body).decode("utf-8")

        if isinstance(body, str):
            body = json.loads(body)

        if not isinstance(body, dict):
            return None, "Invalid JSON body"

        return body, None
    except json.JSONDecodeError:
        return None, "Invalid JSON format"


class S3LocationError(Exception):
    """Raised when S3 location cannot be resolved from the request."""


def _get_s3_location(request: dict[str, Any], job_record: dict[str, Any]) -> tuple[str, str]:
    """Extract S3 bucket and key from request or job record."""
    s3_url = request.get("s3_url")
    if s3_url:
        try:
            return _parse_s3_url(str(s3_url))
        except ValueError as exc:
            raise S3LocationError(str(exc)) from exc

    bucket = str(request.get("s3_bucket") or job_record.get("s3_bucket") or RESUME_BUCKET or "")
    key = str(request.get("s3_key") or job_record.get("s3_key") or "")
    if not bucket or not key:
        raise S3LocationError("Provide 's3_url' or both 's3_bucket' and 's3_key'")
    return bucket, key


def _is_upstream_unavailable_error(exc: Exception) -> bool:
    """Detect transient upstream 503 errors (e.g., Gemini high demand)."""
    message = str(exc)
    type_name = type(exc).__name__

    status_code = getattr(exc, "status_code", None)
    code = getattr(exc, "code", None)
    if http.HTTPStatus.SERVICE_UNAVAILABLE in {status_code, code}:
        return True

    if "503" not in message:
        return False

    unavailable_markers = (
        "UNAVAILABLE",
        "high demand",
        "ServerError",
        "'code': 503",
        '"code": 503',
    )
    return type_name == "ServerError" or any(marker in message for marker in unavailable_markers)


def _handle_analysis_error(
    job_id: str | None,
    exc: Exception,
    *,
    event: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Handle analysis errors and return appropriate response."""
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "Unknown")
        message = f"S3 access error: {code}"
        if job_id:
            _update_job_status(job_id, "failed", error=message)
        return api_response(500, {"error": message}, event=event)

    if _is_upstream_unavailable_error(exc):
        message = (
            "Analysis service is temporarily unavailable due to high demand. "
            "Please try again in a few minutes."
        )
        if job_id:
            _update_job_status(job_id, "failed", error=message)
        return api_response(503, {"error": message, "type": "ServiceUnavailable"}, event=event)

    logger.exception("Analysis handler failed")
    if job_id:
        _update_job_status(job_id, "failed", error=str(exc))
    return api_response(500, {"error": "Internal server error"}, event=event)


def lambda_handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Lambda handler for resume analysis.

    Analyzes a resume PDF against a job description using Gemini AI.
    Accepts either an S3 URL or bucket/key combination.
    """
    request, error = _extract_request(event)
    if error:
        return api_response(400, {"error": error}, event=event)

    if request is None:
        return api_response(400, {"error": "No body in request"}, event=event)

    job_id = request.get("job_id")
    job_description = str(request.get("job_description", "General resume analysis"))
    job_record = _get_job_record(str(job_id)) if job_id else {}

    if "job_description" not in request and job_record.get("job_description"):
        job_description = str(job_record["job_description"])

    try:
        bucket, key = _get_s3_location(request, job_record)

        if job_id:
            _update_job_status(job_id, "processing")

        pdf_bytes = _download_pdf_bytes(bucket, key)
        analysis = analyze_resume_pdf(pdf_bytes, job_description)

        if job_id:
            _update_job_status(job_id, "completed", analysis_result=analysis)

        return api_response(
            200,
            {
                "job_id": job_id,
                "s3_bucket": bucket,
                "s3_key": key,
                "analysis_result": analysis,
            },
            event=event,
        )
    except S3LocationError as exc:
        return api_response(400, {"error": str(exc)}, event=event)
    except Exception as exc:
        logger.exception("Analysis handler failed")
        return _handle_analysis_error(job_id, exc, event=event)
