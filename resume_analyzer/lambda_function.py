import base64
import binascii
import http
import json
import logging
import os
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import unquote, urlparse

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from google.genai import Client, types

try:
    from resume_analyzer.utils import (
        ACCOUNT_ID,
        RESUME_BUCKET,
        api_response,
        get_lambda_client,
        get_results_table,
        get_s3_client,
        normalize_analysis_result,
    )
except ImportError:
    from utils import (
        ACCOUNT_ID,
        RESUME_BUCKET,
        api_response,
        get_lambda_client,
        get_results_table,
        get_s3_client,
        normalize_analysis_result,
    )

logger = logging.getLogger(__name__)

GEMINI_MODEL_ID = os.environ.get("GEMINI_MODEL_ID", "gemini-3-flash-preview")
PDF_MAGIC_BYTES = b"%PDF"
MAX_PDF_SIZE = int(os.environ.get("MAX_UPLOAD_BYTES", str(10 * 1024 * 1024)))
MAX_JOB_DESCRIPTION_LENGTH = int(os.environ.get("MAX_JOB_DESCRIPTION_LENGTH", "5000"))
MAX_GEMINI_RETRIES = 3
RETRY_BASE_DELAY_SECONDS = 1.0
INTERNAL_WORKER_SOURCE = "resume-analyzer.worker"

_CLIENT_CACHE: dict[str, Any | str | None] = {
    "genai_client": None,
    "genai_client_api_key": None,
    "secrets_client": None,
    "cached_secret_arn": None,
    "cached_google_api_key": None,
}

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
        "education": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "institution": {"type": "string"},
                    "degree": {"type": "string"},
                    "field": {"type": "string"},
                    "graduation_date": {"type": "string"},
                },
                "required": [
                    "institution",
                    "degree",
                    "field",
                    "graduation_date",
                ],
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
        "education",
        "strengths",
        "gaps",
        "recommendations",
    ],
    "additionalProperties": False,
}


class UploadNotReadyError(Exception):
    """Raised when the uploaded PDF is not yet ready for analysis."""


class JobTransitionError(Exception):
    """Raised when a conditional job status transition fails."""


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


def _build_s3_kwargs(bucket: str, key: str) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"Bucket": bucket, "Key": key}
    if ACCOUNT_ID:
        kwargs["ExpectedBucketOwner"] = ACCOUNT_ID
    return kwargs


def _ensure_pdf_object_ready(bucket: str, key: str) -> None:
    try:
        metadata = get_s3_client().head_object(**_build_s3_kwargs(bucket, key))
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "")
        if error_code in {"404", "NoSuchKey", "NotFound"}:
            raise UploadNotReadyError(
                "Uploaded resume not found yet. Please finish the upload and try again."
            ) from exc
        raise

    content_length = metadata.get("ContentLength")
    if isinstance(content_length, int):
        if content_length <= 0:
            raise UploadNotReadyError(
                "Uploaded resume is empty. Please upload a valid PDF and try again."
            )
        if content_length > MAX_PDF_SIZE:
            raise ValueError(f"PDF exceeds maximum size of {MAX_PDF_SIZE} bytes")


def _download_pdf_bytes(bucket: str, key: str) -> bytes:
    obj = get_s3_client().get_object(**_build_s3_kwargs(bucket, key))
    data = obj["Body"].read()

    if not data:
        raise ValueError("Downloaded PDF is empty")
    if len(data) > MAX_PDF_SIZE:
        raise ValueError(f"PDF exceeds maximum size of {MAX_PDF_SIZE} bytes")
    if not data.startswith(PDF_MAGIC_BYTES):
        raise ValueError("File does not appear to be a valid PDF")
    return data


def _get_secrets_client() -> Any:
    if _CLIENT_CACHE["secrets_client"] is None:
        _CLIENT_CACHE["secrets_client"] = boto3.client("secretsmanager")
    return _CLIENT_CACHE["secrets_client"]


def _extract_google_api_key(secret_value: str) -> str:
    text = secret_value.strip()
    if not text:
        raise RuntimeError("Secrets Manager secret does not contain a Google API key")

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return text

    if isinstance(parsed, str) and parsed.strip():
        return parsed.strip()

    if isinstance(parsed, dict):
        for field_name in ("GOOGLE_API_KEY", "google_api_key", "api_key"):
            value = parsed.get(field_name)
            if isinstance(value, str) and value.strip():
                return value.strip()

    raise RuntimeError("Secrets Manager secret does not contain a usable Google API key")


def _get_google_api_key() -> str:
    secret_arn = os.environ.get("GOOGLE_API_KEY_SECRET_ARN", "").strip()
    if secret_arn:
        if (
            _CLIENT_CACHE["cached_google_api_key"] is None
            or _CLIENT_CACHE["cached_secret_arn"] != secret_arn
        ):
            response = _get_secrets_client().get_secret_value(SecretId=secret_arn)
            secret_string = str(response.get("SecretString") or "")
            _CLIENT_CACHE["cached_google_api_key"] = _extract_google_api_key(secret_string)
            _CLIENT_CACHE["cached_secret_arn"] = secret_arn
        return str(_CLIENT_CACHE["cached_google_api_key"])

    env_api_key = os.environ.get("GOOGLE_API_KEY", "").strip()
    if env_api_key:
        _CLIENT_CACHE["cached_secret_arn"] = None
        _CLIENT_CACHE["cached_google_api_key"] = None
        return env_api_key

    raise RuntimeError(
        "GOOGLE_API_KEY_SECRET_ARN or GOOGLE_API_KEY environment variable not configured"
    )


def _get_genai_client() -> Any:
    api_key = _get_google_api_key()
    if _CLIENT_CACHE["genai_client"] is None or _CLIENT_CACHE["genai_client_api_key"] != api_key:
        _CLIENT_CACHE["genai_client"] = Client(api_key=api_key)
        _CLIENT_CACHE["genai_client_api_key"] = api_key
    return _CLIENT_CACHE["genai_client"]


def _build_analysis_prompt(job_description: str) -> str:
    return (
        "You are an expert technical recruiter and resume analyst. "
        "Analyze this resume PDF against the provided job description and return JSON only "
        "that exactly matches the supplied schema. "
        "Use empty strings or empty arrays when the resume does not provide enough evidence. "
        "Do not invent qualifications, employers, dates, education history, or certifications.\n\n"
        "Field guidance:\n"
        "- name: candidate full name.\n"
        "- contact_info: email, phone, location, linkedin URL when available.\n"
        "- summary: concise job-targeted summary grounded in the resume.\n"
        "- skills: normalized list of relevant hard and soft skills.\n"
        "- experience: each role with company, role, duration, and highlights.\n"
        "- education: institution, degree, field, and graduation date for each entry.\n"
        "- strengths: concrete ways the resume matches the job description.\n"
        "- gaps: missing or weak evidence relative to the job description.\n"
        "- recommendations: specific next steps tailored to this candidate and role.\n\n"
        f"Job Description:\n{job_description}"
    )


def analyze_resume_pdf(pdf_bytes: bytes, job_description: str) -> dict[str, Any]:
    """Calls Gemini 3 Flash Preview with native PDF input and strict JSON schema output."""
    response_schema = RESUME_ANALYSIS_RESPONSE_SCHEMA
    prompt = _build_analysis_prompt(job_description)
    contents = [
        types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"),
        prompt,
    ]
    del pdf_bytes

    response: Any | None = None
    for attempt in range(MAX_GEMINI_RETRIES):
        try:
            response = _get_genai_client().models.generate_content(
                model=GEMINI_MODEL_ID,
                contents=contents,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_json_schema=response_schema,
                    temperature=0.2,
                ),
            )
            break
        except Exception as exc:
            if _is_retryable_upstream_error(exc) and attempt < MAX_GEMINI_RETRIES - 1:
                delay = RETRY_BASE_DELAY_SECONDS * (2**attempt)
                logger.warning(
                    "Gemini request hit a retryable upstream error; retrying in %.1fs (attempt %d/%d)",
                    delay,
                    attempt + 1,
                    MAX_GEMINI_RETRIES,
                )
                time.sleep(delay)
                continue
            raise

    if response is None:
        raise RuntimeError("Gemini did not return a response")

    text = getattr(response, "text", None)
    if not text:
        raise RuntimeError("Gemini returned an empty response")

    try:
        return normalize_analysis_result(json.loads(text), strict=True)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Gemini returned non-JSON output: {text[:200]}") from exc


def _conditional_check_failed(exc: Exception) -> bool:
    return (
        isinstance(exc, ClientError)
        and exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"
    )


def _update_job_status(
    job_id: str | None,
    status_value: str,
    analysis_result: dict[str, Any] | None = None,
    error: str | None = None,
    *,
    expected_statuses: set[str] | None = None,
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
    condition = "attribute_exists(job_id)"

    if analysis_result is not None:
        expression += ", analysis_result = :result, completed_at = :completed"
        values[":result"] = analysis_result
        values[":completed"] = datetime.now(UTC).isoformat().replace("+00:00", "Z")

    if error:
        expression += ", #error = :error"
        names["#error"] = "error"
        values[":error"] = error

    if expected_statuses:
        placeholders: list[str] = []
        for index, expected in enumerate(sorted(expected_statuses)):
            placeholder = f":expected{index}"
            placeholders.append(placeholder)
            values[placeholder] = expected
        condition += f" AND #status IN ({', '.join(placeholders)})"

    try:
        kwargs: dict[str, Any] = {
            "Key": {"job_id": job_id},
            "UpdateExpression": expression,
            "ExpressionAttributeNames": names,
            "ExpressionAttributeValues": values,
            "ConditionExpression": condition,
        }
        table.update_item(**kwargs)
    except Exception as exc:
        if _conditional_check_failed(exc):
            raise JobTransitionError("Job is not in an expected state") from exc
        logger.exception("Failed to update DynamoDB status")


def _mark_job_queued(job_id: str) -> bool:
    try:
        _update_job_status(job_id, "queued", expected_statuses={"upload_pending", "failed"})
        return True
    except JobTransitionError:
        return False


def _claim_job_for_processing(job_id: str) -> bool:
    try:
        _update_job_status(job_id, "processing", expected_statuses={"queued"})
        return True
    except JobTransitionError:
        return False


def _set_job_failed(job_id: str | None, error: str) -> None:
    try:
        _update_job_status(job_id, "failed", error=error)
    except JobTransitionError:
        logger.info("Skipping failed status update because the job no longer exists")


def _set_job_completed(job_id: str, analysis_result: dict[str, Any]) -> None:
    try:
        _update_job_status(
            job_id,
            "completed",
            analysis_result=analysis_result,
            expected_statuses={"processing"},
        )
    except JobTransitionError:
        logger.warning("Skipping completed status update because the job was not processing")


def _invoke_worker(job_id: str) -> None:
    function_name = os.environ.get("ANALYZE_FUNCTION_NAME") or os.environ.get(
        "AWS_LAMBDA_FUNCTION_NAME"
    )
    if not function_name:
        raise RuntimeError("ANALYZE_FUNCTION_NAME environment variable not configured")

    get_lambda_client().invoke(
        FunctionName=function_name,
        InvocationType="Event",
        Payload=json.dumps({"source": INTERNAL_WORKER_SOURCE, "job_id": job_id}).encode("utf-8"),
    )


def _build_status_url(event: dict[str, Any], job_id: str) -> str:
    headers = event.get("headers") or {}
    host = headers.get("Host") or headers.get("host")
    stage = (event.get("requestContext") or {}).get("stage")
    if host and stage:
        return f"https://{host}/{stage}/status/{job_id}"
    return f"/status/{job_id}"


def _job_status_response(
    event: dict[str, Any],
    job_id: str,
    job_record: dict[str, Any],
    *,
    status_code: int = 202,
    message: str = "Analysis queued",
) -> dict[str, Any]:
    return api_response(
        status_code,
        {
            "job_id": job_id,
            "status": job_record.get("status", "queued"),
            "filename": job_record.get("filename"),
            "status_url": _build_status_url(event, job_id),
            "message": message,
        },
        event=event,
    )


def _refresh_job_record(job_id: str) -> dict[str, Any]:
    return _get_job_record(job_id)


class S3LocationError(Exception):
    """Raised when S3 location cannot be resolved from the request."""


def _stored_s3_location(job_record: dict[str, Any]) -> tuple[str, str]:
    stored_bucket = str(job_record.get("s3_bucket") or "")
    stored_key = str(job_record.get("s3_key") or "")
    if not stored_bucket or not stored_key:
        raise S3LocationError("Job record is missing its upload location")
    return stored_bucket, stored_key


def _requested_s3_location(request: dict[str, Any]) -> tuple[str, str] | None:
    if request.get("s3_url"):
        return _parse_s3_url(str(request["s3_url"]))

    if not (request.get("s3_bucket") or request.get("s3_key")):
        return None

    requested_bucket = str(request.get("s3_bucket") or "")
    requested_key = str(request.get("s3_key") or "")
    if not requested_bucket or not requested_key:
        raise S3LocationError("Provide both 's3_bucket' and 's3_key'")
    return requested_bucket, requested_key


def _validate_stored_s3_location(job_id: str, bucket: str, key: str) -> None:
    if RESUME_BUCKET and bucket != RESUME_BUCKET:
        raise S3LocationError("Job is associated with an unexpected bucket")

    expected_prefix = f"uploads/{job_id}/"
    if not key.startswith(expected_prefix):
        raise S3LocationError("Job upload key has an unexpected prefix")


def _validate_s3_location(
    request: dict[str, Any],
    job_id: str,
    job_record: dict[str, Any],
) -> tuple[str, str]:
    if not job_record:
        raise S3LocationError("Job not found")

    stored_bucket, stored_key = _stored_s3_location(job_record)
    requested_location = _requested_s3_location(request)

    if requested_location and requested_location != (stored_bucket, stored_key):
        raise S3LocationError("Requested S3 location does not match the job record")

    _validate_stored_s3_location(job_id, stored_bucket, stored_key)
    return stored_bucket, stored_key


def _parse_worker_event(event: dict[str, Any]) -> str | None:
    if event.get("source") == INTERNAL_WORKER_SOURCE and isinstance(event.get("job_id"), str):
        return str(event["job_id"])
    return None


def _handle_worker_event(event: dict[str, Any]) -> dict[str, Any]:
    job_id = str(event["job_id"])
    job_record = _get_job_record(job_id)
    if not job_record:
        logger.warning("Worker invoked for a missing job")
        return {"status": "missing"}

    if not _claim_job_for_processing(job_id):
        logger.info("Worker skipped a job because it was not queued")
        return {"status": "skipped"}

    try:
        bucket, key = _validate_s3_location({}, job_id, job_record)
        _ensure_pdf_object_ready(bucket, key)
        pdf_bytes = _download_pdf_bytes(bucket, key)
        analysis = analyze_resume_pdf(pdf_bytes, _resolve_job_description({}, job_record))
        _set_job_completed(job_id, analysis)
        return {"status": "completed"}
    except UploadNotReadyError as exc:
        _set_job_failed(job_id, str(exc))
        return {"status": "failed", "error": str(exc)}
    except ValueError as exc:
        _set_job_failed(job_id, str(exc))
        return {"status": "failed", "error": str(exc)}
    except (
        AttributeError,
        BotoCoreError,
        ClientError,
        KeyError,
        RuntimeError,
        TypeError,
    ) as exc:
        _handle_analysis_error(job_id, exc)
        return {"status": "failed", "error": str(exc)}


def _queue_analysis_request(
    event: dict[str, Any],
    request: dict[str, Any],
    job_id: str,
    job_record: dict[str, Any],
) -> dict[str, Any]:
    try:
        _validate_s3_location(request, job_id, job_record)
    except S3LocationError as exc:
        status_code = 404 if str(exc) == "Job not found" else 400
        return api_response(status_code, {"error": str(exc)}, event=event)
    except ValueError as exc:
        return api_response(400, {"error": str(exc)}, event=event)

    status = str(job_record.get("status", ""))
    if status == "completed":
        return _job_status_response(
            event,
            job_id,
            job_record,
            status_code=200,
            message="Analysis already completed",
        )
    if status in {"queued", "processing"}:
        return _job_status_response(
            event,
            job_id,
            job_record,
            status_code=202,
            message="Analysis already queued",
        )

    queued = _mark_job_queued(job_id)
    refreshed = _refresh_job_record(job_id) or {**job_record, "status": "queued"}
    if queued:
        _invoke_worker(job_id)
        refreshed["status"] = "queued"

    return _job_status_response(
        event,
        job_id,
        refreshed,
        status_code=202,
        message="Analysis queued" if queued else "Analysis already queued",
    )


def _validate_analyze_request(request: dict[str, Any]) -> str | None:
    job_id = request.get("job_id")
    if not isinstance(job_id, str) or not job_id.strip():
        return "job_id is required"
    if (
        "job_description" in request
        and len(str(request["job_description"])) > MAX_JOB_DESCRIPTION_LENGTH
    ):
        return f"job_description exceeds maximum length of {MAX_JOB_DESCRIPTION_LENGTH} characters"
    return None


def _get_job_record(job_id: str) -> dict[str, Any]:
    try:
        table = get_results_table()
        item = table.get_item(Key={"job_id": job_id}).get("Item")
        return item if isinstance(item, dict) else {}
    except Exception:
        logger.exception("Failed to read DynamoDB job record")
        return {}


def _extract_request(event: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    if "body" not in event:
        return None, "No body in request"

    try:
        body: Any = event.get("body", "")
        if event.get("isBase64Encoded", False):
            body = base64.b64decode(body, validate=True).decode("utf-8")

        if isinstance(body, str):
            body = json.loads(body)

        if not isinstance(body, dict):
            return None, "Invalid JSON body"

        return body, None
    except (binascii.Error, UnicodeDecodeError):
        return None, "Invalid base64-encoded body"
    except json.JSONDecodeError:
        return None, "Invalid JSON format"


def _is_retryable_upstream_error(exc: Exception) -> bool:
    """Detect transient upstream 429/503 errors that should be retried."""
    status_code = getattr(exc, "status_code", None)
    code = getattr(exc, "code", None)

    codes = {candidate for candidate in (status_code, code) if candidate is not None}
    if http.HTTPStatus.SERVICE_UNAVAILABLE in codes:
        return True
    if http.HTTPStatus.TOO_MANY_REQUESTS in codes:
        return True

    normalized_codes = {str(candidate).strip() for candidate in codes}
    if {"503", "429"} & normalized_codes:
        return True

    type_name = type(exc).__name__
    if type_name in {
        "ServerError",
        "ServiceUnavailable",
        "TooManyRequests",
        "RateLimitError",
        "ResourceExhausted",
    }:
        return True

    message = str(exc).lower()
    retryable_markers = (
        "503",
        "429",
        "high demand",
        "service unavailable",
        "too many requests",
        "rate limit",
        "quota",
    )
    return any(marker in message for marker in retryable_markers)


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
            _set_job_failed(job_id, message)
        return api_response(500, {"error": message}, event=event)

    if _is_retryable_upstream_error(exc):
        message = (
            "Analysis service is temporarily unavailable due to high demand or rate limiting. "
            "Please try again in a few minutes."
        )
        if job_id:
            _set_job_failed(job_id, message)
        return api_response(503, {"error": message, "type": "ServiceUnavailable"}, event=event)

    logger.exception("Analysis handler failed")
    if job_id:
        _set_job_failed(job_id, str(exc))
    return api_response(500, {"error": "Internal server error"}, event=event)


def _resolve_job_description(request: dict[str, Any], job_record: dict[str, Any]) -> str:
    if "job_description" in request:
        return str(request.get("job_description", "General resume analysis"))
    if job_record.get("job_description"):
        return str(job_record["job_description"])
    return "General resume analysis"


def lambda_handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Lambda handler for queuing and executing resume analysis."""
    worker_job_id = _parse_worker_event(event)
    if worker_job_id:
        return _handle_worker_event(event)

    request, error = _extract_request(event)
    if error:
        return api_response(400, {"error": error}, event=event)

    if request is None:
        return api_response(400, {"error": "No body in request"}, event=event)

    validation_error = _validate_analyze_request(request)
    if validation_error:
        return api_response(400, {"error": validation_error}, event=event)

    job_id = str(request["job_id"]).strip()
    job_record = _get_job_record(job_id)
    return _queue_analysis_request(event, request, job_id, job_record)
