import base64
import json
import logging
import os
from datetime import UTC, datetime
from typing import Any
from urllib.parse import unquote, urlparse

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

s3_client = boto3.client("s3")
dynamodb: Any = boto3.resource("dynamodb")

GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")
RESUME_BUCKET = os.environ.get("RESUME_BUCKET")
RESULTS_TABLE = os.environ.get("RESULTS_TABLE")
ACCOUNT_ID = os.environ.get("AWS_ACCOUNT_ID", "")
CORS_ALLOW_ORIGIN = os.environ.get("CORS_ALLOW_ORIGIN", "*")
GEMINI_MODEL_ID = os.environ.get("GEMINI_MODEL_ID", "gemini-3-flash-preview")

results_table: Any | None = dynamodb.Table(RESULTS_TABLE) if RESULTS_TABLE else None

# JSON Schema for structured output. This schema is compatible with Pydantic models.
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
        "gaps",
        "recommendations",
    ],
    "additionalProperties": False,
}


def _response(status_code: int, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": CORS_ALLOW_ORIGIN,
        },
        "body": json.dumps(payload),
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
            if len(parts) != 2:
                raise ValueError("Invalid S3 path-style URL")
            return parts[0], unquote(parts[1])

        if len(host_parts) >= 3 and host_parts[1] == "s3":
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
    from google import genai  # noqa: PLC0415  # type: ignore[import-not-found]

    return genai.Client(api_key=GOOGLE_API_KEY)


def _get_genai_types() -> Any:
    from google.genai import types  # noqa: PLC0415

    return types


def analyze_resume_pdf(pdf_bytes: bytes, job_description: str) -> dict[str, Any]:
    """Calls Gemini 3 Flash with native PDF input and strict JSON schema output."""
    if not GOOGLE_API_KEY:
        raise RuntimeError("GOOGLE_API_KEY environment variable not configured")

    types = _get_genai_types()

    # Keep the schema object in `response_schema`; SDK sends JSON Schema via `response_json_schema`.
    response_schema = RESUME_ANALYSIS_RESPONSE_SCHEMA
    prompt = (
        "Analyze this resume PDF against the provided job description and return JSON only. "
        "If a field is unknown, return an empty string or empty list.\n\n"
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
            # JSON Schema registration for structured outputs.
            response_json_schema=response_schema,
            temperature=0.2,
        ),
    )

    text = getattr(response, "text", None)
    if not text:
        raise RuntimeError("Gemini returned an empty response")

    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Gemini returned non-JSON output: {text[:200]}") from exc


def _update_job_status(
    job_id: str | None,
    status_value: str,
    analysis_result: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    if not job_id or not results_table:
        return

    expression = "SET #status = :status"
    names = {"#status": "status"}
    values: dict[str, Any] = {":status": status_value}

    if analysis_result is not None:
        expression += ", analysis_result = :result, completed_at = :completed"
        values[":result"] = analysis_result
        values[":completed"] = datetime.now(UTC).isoformat().replace("+00:00", "Z")

    if error:
        expression += ", error = :error"
        values[":error"] = error

    try:
        results_table.update_item(
            Key={"job_id": job_id},
            UpdateExpression=expression,
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
    except Exception:
        logger.exception("Failed to update DynamoDB status for job_id=%s", job_id)


def _get_job_record(job_id: str) -> dict[str, Any]:
    if not results_table:
        return {}
    try:
        item = results_table.get_item(Key={"job_id": job_id}).get("Item")
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


def _get_s3_location(
    request: dict[str, Any], job_record: dict[str, Any]
) -> tuple[str, str] | dict[str, Any]:
    """Extract S3 bucket and key from request or job record.

    Returns either (bucket, key) tuple or error response dict.
    """
    s3_url = request.get("s3_url")
    if s3_url:
        bucket, key = _parse_s3_url(str(s3_url))
        return bucket, key

    bucket = str(request.get("s3_bucket") or job_record.get("s3_bucket") or RESUME_BUCKET or "")
    key = str(request.get("s3_key") or job_record.get("s3_key") or "")
    if not bucket or not key:
        return _response(400, {"error": "Provide 's3_url' or both 's3_bucket' and 's3_key'"})
    return bucket, key


def _handle_analysis_error(job_id: str | None, exc: Exception) -> dict[str, Any]:
    """Handle analysis errors and return appropriate response."""
    if isinstance(exc, ClientError):
        message = f"S3 access error: {exc.response.get('Error', {}).get('Code', 'Unknown')}"
        if job_id:
            _update_job_status(job_id, "failed", error=message)
        return _response(500, {"error": message})

    logger.exception("Analysis handler failed")
    if job_id:
        _update_job_status(job_id, "failed", error=str(exc))
    return _response(500, {"error": str(exc), "type": type(exc).__name__})


def lambda_handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Lambda handler for resume analysis.

    Analyzes a resume PDF against a job description using Gemini AI.
    Accepts either an S3 URL or bucket/key combination.
    """
    request, error = _extract_request(event)
    if error:
        return _response(400, {"error": error})

    assert request is not None
    job_id = request.get("job_id")
    job_description = str(request.get("job_description", "General resume analysis"))
    job_record = _get_job_record(str(job_id)) if job_id else {}

    if "job_description" not in request and job_record.get("job_description"):
        job_description = str(job_record["job_description"])

    try:
        location = _get_s3_location(request, job_record)
        if isinstance(location, dict):
            return location
        bucket, key = location

        if job_id:
            _update_job_status(job_id, "processing")

        pdf_bytes = _download_pdf_bytes(bucket, key)
        analysis = analyze_resume_pdf(pdf_bytes, job_description)

        if job_id:
            _update_job_status(job_id, "completed", analysis_result=analysis)

        return _response(
            200,
            {
                "job_id": job_id,
                "s3_bucket": bucket,
                "s3_key": key,
                "analysis_result": analysis,
            },
        )
    except (ClientError, Exception) as exc:
        return _handle_analysis_error(job_id, exc)
