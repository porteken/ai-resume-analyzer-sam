"""Analyze uploaded resumes and coordinate asynchronous processing jobs."""

import base64
import binascii
import http
import importlib
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import unquote, urlparse

from botocore.exceptions import BotoCoreError, ClientError

try:
    _utils = importlib.import_module("resume_analyzer.utils")
except ImportError:
    _utils = importlib.import_module("utils")

ACCOUNT_ID = _utils.ACCOUNT_ID
MAX_JOB_DESCRIPTION_LENGTH = _utils.MAX_JOB_DESCRIPTION_LENGTH
RESUME_BUCKET = _utils.RESUME_BUCKET
api_response = _utils.api_response
get_lambda_client = _utils.get_lambda_client
get_results_table = _utils.get_results_table
get_s3_client = _utils.get_s3_client
get_secrets_client = _utils.get_secrets_client
normalize_analysis_result = _utils.normalize_analysis_result

logger = logging.getLogger(__name__)

GEMINI_MODEL_ID = os.environ.get("GEMINI_MODEL_ID", "gemini-3-flash-preview")
GEMINI_THINKING_LEVEL = os.environ.get("GEMINI_THINKING_LEVEL", "").strip().upper()
GEMINI_MAX_OUTPUT_TOKENS = int(os.environ.get("GEMINI_MAX_OUTPUT_TOKENS", "8192"))
PDF_MAGIC_BYTES = b"%PDF"
MAX_PDF_SIZE = int(os.environ.get("MAX_UPLOAD_BYTES", str(10 * 1024 * 1024)))
MAX_GEMINI_RETRIES = 3
RETRY_BASE_DELAY_SECONDS = 1.0
INTERNAL_WORKER_SOURCE = "resume-analyzer.worker"

GEMINI_TIMEOUT_FLOOR_MS = 5_000
MIN_GEMINI_TIMEOUT_MS = 3_000
DEFAULT_GEMINI_TIMEOUT_MS = 60_000

MAX_LIST_ITEMS = 8
MAX_EXPERIENCE_ENTRIES = 6
MAX_EDUCATION_ENTRIES = 4
MAX_HIGHLIGHTS_PER_ROLE = 5


_GENAI_MODULE_CACHE: dict[str, Any] = {"module": None}


def _genai() -> Any:
    """Return the google.genai module, importing it on first use.

    google.genai is only needed on the worker path, not on the synchronous API
    path that just queues a job, so the import is deferred to keep it off the
    cold start of an invocation that never calls Gemini.
    """
    module = _GENAI_MODULE_CACHE["module"]
    if module is None:
        module = importlib.import_module("google.genai")
        _GENAI_MODULE_CACHE["module"] = module
    return module


CLIENT_CACHE_KEYS = (
    "genai_client",
    "genai_client_api_key",
    "cached_secret_arn",
    "cached_google_api_key",
)

_CLIENT_CACHE: dict[str, Any | str | None] = dict.fromkeys(CLIENT_CACHE_KEYS)


def reset_cached_clients() -> None:
    """Reset cached Gemini clients and the resolved API key used by tests."""
    _CLIENT_CACHE.update(dict.fromkeys(CLIENT_CACHE_KEYS))
    _utils.reset_cached_clients()


STRING_FIELDS = ("name", "summary")
STRING_LIST_FIELDS = ("skills", "strengths", "gaps", "recommendations")
CONTACT_FIELDS = ("email", "phone", "location", "linkedin")
EXPERIENCE_FIELDS = ("company", "role", "duration")
EDUCATION_FIELDS = ("institution", "degree", "field", "graduation_date")


def _string_schema() -> dict[str, str]:
    return {"type": "string"}


def _string_list_schema(max_items: int) -> dict[str, Any]:
    return {"type": "array", "items": _string_schema(), "maxItems": max_items}


def _object_schema(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def _object_list_schema(properties: dict[str, Any], max_items: int) -> dict[str, Any]:
    return {"type": "array", "items": _object_schema(properties), "maxItems": max_items}


def _analysis_response_schema() -> dict[str, Any]:
    properties: dict[str, Any] = {field: _string_schema() for field in STRING_FIELDS}
    properties["contact_info"] = _object_schema(
        {field: _string_schema() for field in CONTACT_FIELDS}
    )
    properties.update({field: _string_list_schema(MAX_LIST_ITEMS) for field in STRING_LIST_FIELDS})
    properties["experience"] = _object_list_schema(
        {
            **{field: _string_schema() for field in EXPERIENCE_FIELDS},
            "highlights": _string_list_schema(MAX_HIGHLIGHTS_PER_ROLE),
        },
        MAX_EXPERIENCE_ENTRIES,
    )
    properties["education"] = _object_list_schema(
        {field: _string_schema() for field in EDUCATION_FIELDS},
        MAX_EDUCATION_ENTRIES,
    )
    return _object_schema(properties)


RESUME_ANALYSIS_RESPONSE_SCHEMA = _analysis_response_schema()


class UploadNotReadyError(Exception):
    """Raised when the uploaded PDF is not yet ready for analysis."""


class JobTransitionError(Exception):
    """Raised when a conditional job status transition fails."""


ANALYSIS_INPUT_ERRORS = (UploadNotReadyError, ValueError)
FAILED_WORKER_EXCEPTIONS = (
    AttributeError,
    BotoCoreError,
    ClientError,
    KeyError,
    RuntimeError,
    TypeError,
)
S3_NOT_FOUND_CODES = frozenset({"404", "NoSuchKey", "NotFound"})
GOOGLE_API_KEY_FIELDS = ("GOOGLE_API_KEY", "google_api_key", "api_key")
RETRYABLE_STATUS_CODES = frozenset(
    {
        http.HTTPStatus.SERVICE_UNAVAILABLE,
        http.HTTPStatus.TOO_MANY_REQUESTS,
        "503",
        "429",
    }
)
RETRYABLE_ERROR_TYPES = frozenset(
    {
        "ServerError",
        "ServiceUnavailable",
        "TooManyRequests",
        "RateLimitError",
        "ResourceExhausted",
    }
)
RETRYABLE_MESSAGE_MARKERS = (
    "503",
    "429",
    "high demand",
    "service unavailable",
    "too many requests",
    "rate limit",
    "quota",
)
VIRTUAL_HOSTED_S3_PARTS_MIN = 3
PATH_STYLE_S3_PARTS = 2


def _client_error_code(exc: ClientError, default: str = "Unknown") -> str:
    response = exc.response or {}
    error = response.get("Error", {})
    if isinstance(error, dict):
        return str(error.get("Code", default))
    return default


def _parse_s3_url(s3_url: str) -> tuple[str, str]:
    parsed = urlparse(s3_url)

    if parsed.scheme == "s3" and parsed.netloc:
        return parsed.netloc, _decoded_s3_key(parsed.path)

    if parsed.scheme in {"http", "https"}:
        location = _s3_location_from_amazon_url(parsed.netloc, parsed.path)
        if location:
            return location

    raise ValueError("s3_url must be a valid s3:// or https://...amazonaws.com URL")


def _decoded_s3_key(path: str) -> str:
    return unquote(path.lstrip("/"))


def _s3_location_from_amazon_url(host: str, path: str) -> tuple[str, str] | None:
    if not host.endswith("amazonaws.com"):
        return None

    host_parts = host.split(".")
    key_path = path.lstrip("/")
    if host_parts[0] == "s3":
        return _path_style_s3_location(key_path)
    if len(host_parts) >= VIRTUAL_HOSTED_S3_PARTS_MIN and host_parts[1] == "s3":
        return host_parts[0], unquote(key_path)
    return None


def _path_style_s3_location(path: str) -> tuple[str, str]:
    bucket_and_key = path.split("/", 1)
    if len(bucket_and_key) != PATH_STYLE_S3_PARTS:
        raise ValueError("Invalid S3 path-style URL")
    return bucket_and_key[0], unquote(bucket_and_key[1])


def _build_s3_kwargs(bucket: str, key: str) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"Bucket": bucket, "Key": key}
    if ACCOUNT_ID:
        kwargs["ExpectedBucketOwner"] = ACCOUNT_ID
    return kwargs


def _download_pdf_bytes(bucket: str, key: str) -> bytes:
    try:
        obj = get_s3_client().get_object(**_build_s3_kwargs(bucket, key))
    except ClientError as exc:
        error_code = _client_error_code(exc, default="")
        if error_code in S3_NOT_FOUND_CODES:
            raise UploadNotReadyError(
                "Uploaded resume not found yet. Please finish the upload and try again."
            ) from exc
        raise

    data = obj["Body"].read()
    _validate_pdf_bytes(data)
    return data


def _ensure_pdf_size(size_bytes: int) -> None:
    if size_bytes > MAX_PDF_SIZE:
        raise ValueError(f"PDF exceeds maximum size of {MAX_PDF_SIZE} bytes")


def _validate_pdf_bytes(data: bytes) -> None:
    if not data:
        raise ValueError("Downloaded PDF is empty")
    _ensure_pdf_size(len(data))
    if not data.startswith(PDF_MAGIC_BYTES):
        raise ValueError("File does not appear to be a valid PDF")


def _extract_google_api_key(secret_value: str) -> str:
    text = secret_value.strip()
    if not text:
        raise RuntimeError("Secrets Manager secret does not contain a Google API key")

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return text

    extracted = _api_key_from_secret_payload(parsed)
    if extracted:
        return extracted

    raise RuntimeError("Secrets Manager secret does not contain a usable Google API key")


def _api_key_from_secret_payload(payload: Any) -> str | None:
    if isinstance(payload, str):
        return payload.strip() or None
    if not isinstance(payload, dict):
        return None

    for field_name in GOOGLE_API_KEY_FIELDS:
        value = payload.get(field_name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _get_google_api_key() -> str:
    secret_arn = os.environ.get("GOOGLE_API_KEY_SECRET_ARN", "").strip()
    if secret_arn:
        if (
            _CLIENT_CACHE["cached_google_api_key"] is None
            or _CLIENT_CACHE["cached_secret_arn"] != secret_arn
        ):
            response = get_secrets_client().get_secret_value(SecretId=secret_arn)
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
        _CLIENT_CACHE["genai_client"] = _genai().Client(api_key=api_key)
        _CLIENT_CACHE["genai_client_api_key"] = api_key
    return _CLIENT_CACHE["genai_client"]


def _build_system_instruction() -> str:
    return (
        "You are an expert technical recruiter and resume analyst. "
        "Analyze the attached resume PDF against the provided job description and return JSON "
        "only that exactly matches the supplied schema. "
        "Use empty strings or empty arrays when the resume does not provide enough evidence. "
        "Do not invent qualifications, employers, dates, education history, or certifications. "
        f"Limit skills, strengths, gaps, and recommendations to at most {MAX_LIST_ITEMS} concise "
        f"entries each, experience to at most {MAX_EXPERIENCE_ENTRIES} roles with at most "
        f"{MAX_HIGHLIGHTS_PER_ROLE} highlights per role, and education to at most "
        f"{MAX_EDUCATION_ENTRIES} entries. Keep every entry to one concise line.\n\n"
        "Field guidance:\n"
        "- name: candidate full name.\n"
        "- contact_info: email, phone, location, linkedin URL when available.\n"
        "- summary: concise job-targeted summary grounded in the resume.\n"
        "- skills: normalized list of relevant hard and soft skills.\n"
        "- experience: each role with company, role, duration, and highlights.\n"
        "- education: institution, degree, field, and graduation date for each entry.\n"
        "- strengths: concrete ways the resume matches the job description.\n"
        "- gaps: missing or weak evidence relative to the job description.\n"
        "- recommendations: specific next steps tailored to this candidate and role."
    )


def _build_analysis_prompt(job_description: str) -> str:
    return f"Job Description:\n{job_description}"


def _resolve_gemini_timeout_ms(context: Any) -> int:
    """Bound the per-attempt HTTP timeout by the Lambda's actual remaining time.

    Without this, an unset httpx timeout lets a single hung attempt burn the
    entire function Timeout, killing the container before a failure can be
    recorded and stranding the job at "processing" until DynamoDB TTL expiry.
    """
    get_remaining = getattr(context, "get_remaining_time_in_millis", None)
    if get_remaining is None:
        return DEFAULT_GEMINI_TIMEOUT_MS
    try:
        remaining_ms = int(get_remaining())
    except TypeError, ValueError:
        return DEFAULT_GEMINI_TIMEOUT_MS
    return max(remaining_ms - GEMINI_TIMEOUT_FLOOR_MS, MIN_GEMINI_TIMEOUT_MS)


def _resolve_thinking_config() -> Any | None:
    """Build a ThinkingConfig from GEMINI_THINKING_LEVEL, or None to keep the model default.

    Left unset by default so upgrading the SDK does not silently change
    output quality; set the env var once A/B results justify a level.
    """
    if not GEMINI_THINKING_LEVEL:
        return None
    genai_types = _genai().types
    try:
        level = genai_types.ThinkingLevel[GEMINI_THINKING_LEVEL]
    except KeyError as exc:
        raise RuntimeError(f"Invalid GEMINI_THINKING_LEVEL: {GEMINI_THINKING_LEVEL!r}") from exc
    return genai_types.ThinkingConfig(thinking_level=level)


def _log_gemini_usage(response: Any) -> None:
    usage = getattr(response, "usage_metadata", None)
    if usage is None:
        return
    logger.info(
        "Gemini usage: prompt_tokens=%s thoughts_tokens=%s output_tokens=%s total_tokens=%s",
        getattr(usage, "prompt_token_count", None),
        getattr(usage, "thoughts_token_count", None),
        getattr(usage, "candidates_token_count", None),
        getattr(usage, "total_token_count", None),
    )


def _raise_if_truncated(response: Any) -> None:
    candidates = getattr(response, "candidates", None) or []
    finish_reason = candidates[0].finish_reason if candidates else None
    if finish_reason == _genai().types.FinishReason.MAX_TOKENS:
        raise RuntimeError(
            "Gemini response was truncated by max_output_tokens="
            f"{GEMINI_MAX_OUTPUT_TOKENS}; raise GEMINI_MAX_OUTPUT_TOKENS or tighten the "
            "response schema's maxItems limits."
        )


def analyze_resume_pdf(
    pdf_bytes: bytes,
    job_description: str,
    context: Any = None,
) -> dict[str, Any]:
    """Call Gemini 3 Flash Preview with native PDF input and strict JSON schema output."""
    genai_types = _genai().types
    prompt = _build_analysis_prompt(job_description)
    contents = [prompt, genai_types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf")]
    del pdf_bytes
    thinking_config = _resolve_thinking_config()

    response: Any | None = None
    for attempt in range(MAX_GEMINI_RETRIES):
        try:
            response = _get_genai_client().models.generate_content(
                model=GEMINI_MODEL_ID,
                contents=contents,
                config=genai_types.GenerateContentConfig(
                    system_instruction=_build_system_instruction(),
                    response_mime_type="application/json",
                    response_json_schema=RESUME_ANALYSIS_RESPONSE_SCHEMA,
                    temperature=0.2,
                    max_output_tokens=GEMINI_MAX_OUTPUT_TOKENS,
                    thinking_config=thinking_config,
                    http_options=genai_types.HttpOptions(
                        timeout=_resolve_gemini_timeout_ms(context)
                    ),
                ),
            )
            break
        except Exception as exc:
            if _is_retryable_upstream_error(exc) and attempt < MAX_GEMINI_RETRIES - 1:
                delay = RETRY_BASE_DELAY_SECONDS * 2**attempt
                logger.warning(
                    "Gemini request hit a retryable upstream error; "
                    "retrying in %.1fs (attempt %d/%d)",
                    delay,
                    attempt + 1,
                    MAX_GEMINI_RETRIES,
                )
                time.sleep(delay)
                continue
            raise

    if response is None:
        raise RuntimeError("Gemini did not return a response")

    _log_gemini_usage(response)
    _raise_if_truncated(response)

    text = getattr(response, "text", None)
    if not isinstance(text, str) or not text:
        raise RuntimeError("Gemini returned an empty response")

    try:
        return normalize_analysis_result(json.loads(text), strict=True)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Gemini returned non-JSON output: {text[:200]}") from exc


def _conditional_check_failed(exc: Exception) -> bool:
    return (
        isinstance(exc, ClientError)
        and _client_error_code(exc) == "ConditionalCheckFailedException"
    )


@dataclass(slots=True)
class _JobStatusUpdate:
    """Attributes written by a single job-status transition."""

    status_value: str
    analysis_result: dict[str, Any] | None = None
    error: str | None = None
    expected_statuses: set[str] | None = None
    job_description: str | None = None


def _update_job_status(job_id: str | None, update: _JobStatusUpdate) -> None:
    if not job_id:
        return

    try:
        table = get_results_table()
    except RuntimeError:
        return

    assignments = ["#status = :status"]
    names = {"#status": "status"}
    values: dict[str, Any] = {":status": update.status_value}
    condition = "attribute_exists(job_id)"

    if update.analysis_result is not None:
        _append_completed_update(assignments, values, update.analysis_result)

    if update.error:
        _append_error_update(assignments, names, values, update.error)

    if update.job_description is not None:
        assignments.append("job_description = :job_description")
        values[":job_description"] = update.job_description

    if update.expected_statuses:
        placeholders: list[str] = []
        for index, expected in enumerate(sorted(update.expected_statuses)):
            placeholder = f":expected{index}"
            placeholders.append(placeholder)
            values[placeholder] = expected
        condition += f" AND #status IN ({', '.join(placeholders)})"

    try:
        kwargs: dict[str, Any] = {
            "Key": {"job_id": job_id},
            "UpdateExpression": f"SET {', '.join(assignments)}",
            "ExpressionAttributeNames": names,
            "ExpressionAttributeValues": values,
            "ConditionExpression": condition,
        }
        table.update_item(**kwargs)
    except Exception as exc:
        if _conditional_check_failed(exc):
            raise JobTransitionError("Job is not in an expected state") from exc
        logger.exception("Failed to update DynamoDB status")


def _append_completed_update(
    assignments: list[str],
    values: dict[str, Any],
    analysis_result: dict[str, Any],
) -> None:
    assignments.extend(("analysis_result = :result", "completed_at = :completed"))
    values.update(
        {
            ":result": analysis_result,
            ":completed": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        }
    )


def _append_error_update(
    assignments: list[str],
    names: dict[str, str],
    values: dict[str, Any],
    error: str,
) -> None:
    assignments.append("#error = :error")
    names["#error"] = "error"
    values[":error"] = error


def _mark_job_queued(job_id: str, job_description: str | None = None) -> bool:
    return _try_job_status_transition(
        job_id, "queued", {"upload_pending", "failed"}, job_description=job_description
    )


def _claim_job_for_processing(job_id: str) -> bool:
    return _try_job_status_transition(job_id, "processing", {"queued"})


def _try_job_status_transition(
    job_id: str,
    status_value: str,
    expected_statuses: set[str],
    *,
    job_description: str | None = None,
) -> bool:
    try:
        _update_job_status(
            job_id,
            _JobStatusUpdate(
                status_value,
                expected_statuses=expected_statuses,
                job_description=job_description,
            ),
        )
    except JobTransitionError:
        return False
    else:
        return True


def _set_job_failed(job_id: str | None, error: str) -> None:
    try:
        _update_job_status(job_id, _JobStatusUpdate("failed", error=error))
    except JobTransitionError:
        logger.info("Skipping failed status update because the job no longer exists")


def _set_job_completed(job_id: str, analysis_result: dict[str, Any]) -> None:
    try:
        _update_job_status(
            job_id,
            _JobStatusUpdate(
                "completed",
                analysis_result=analysis_result,
                expected_statuses={"processing"},
            ),
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


def _error_response(
    event: dict[str, Any] | None,
    status_code: int,
    message: str,
    *,
    error_type: str | None = None,
) -> dict[str, Any]:
    body = {"error": message}
    if error_type:
        body["type"] = error_type
    return api_response(status_code, body, event=event)


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


def _handle_worker_event(event: dict[str, Any], context: Any) -> dict[str, Any]:
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
        pdf_bytes = _download_pdf_bytes(bucket, key)
        analysis = analyze_resume_pdf(pdf_bytes, _resolve_job_description({}, job_record), context)
        _set_job_completed(job_id, analysis)
    except ANALYSIS_INPUT_ERRORS as exc:
        return _worker_failure(job_id, exc, mark_failed=True)
    except FAILED_WORKER_EXCEPTIONS as exc:
        _handle_analysis_error(job_id, exc)
        return _worker_failure(job_id, exc)
    else:
        return {"status": "completed"}


def _worker_failure(
    job_id: str,
    exc: Exception,
    *,
    mark_failed: bool = False,
) -> dict[str, str]:
    error = str(exc)
    if mark_failed:
        _set_job_failed(job_id, error)
    return {"status": "failed", "error": error}


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
        return _error_response(event, status_code, str(exc))
    except ValueError as exc:
        return _error_response(event, 400, str(exc))

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

    job_description = str(request["job_description"]) if "job_description" in request else None
    queued = _mark_job_queued(job_id, job_description=job_description)
    refreshed = {**job_record, "status": "queued"}
    if queued:
        _invoke_worker(job_id)

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
    except BotoCoreError, ClientError, RuntimeError:
        logger.exception("Failed to read DynamoDB job record")
        return {}
    except AttributeError, TypeError:
        logger.exception("Unexpected failure reading DynamoDB job record")
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
    except binascii.Error, UnicodeDecodeError:
        return None, "Invalid base64-encoded body"
    except json.JSONDecodeError:
        return None, "Invalid JSON format"
    else:
        return body, None


def _is_retryable_upstream_error(exc: Exception) -> bool:
    """Detect transient upstream 429/503 errors that should be retried."""
    if _retryable_error_code_values(exc) & RETRYABLE_STATUS_CODES:
        return True
    if type(exc).__name__ in RETRYABLE_ERROR_TYPES:
        return True
    return any(marker in str(exc).lower() for marker in RETRYABLE_MESSAGE_MARKERS)


def _retryable_error_code_values(exc: Exception) -> set[Any]:
    values = (getattr(exc, "status_code", None), getattr(exc, "code", None))
    return {value for raw in values if raw is not None for value in (raw, str(raw).strip())}


def _handle_analysis_error(
    job_id: str | None,
    exc: Exception,
    *,
    event: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Handle analysis errors and return the appropriate response."""
    if isinstance(exc, ClientError):
        code = _client_error_code(exc)
        message = f"S3 access error: {code}"
        _fail_job_if_present(job_id, message)
        return _error_response(event, 500, message)

    if _is_retryable_upstream_error(exc):
        message = (
            "Analysis service is temporarily unavailable due to high demand or rate limiting. "
            "Please try again in a few minutes."
        )
        _fail_job_if_present(job_id, message)
        return _error_response(event, 503, message, error_type="ServiceUnavailable")

    logger.exception("Analysis handler failed")
    _fail_job_if_present(job_id, str(exc))
    return _error_response(event, 500, "Internal server error")


def _fail_job_if_present(job_id: str | None, message: str) -> None:
    if job_id:
        _set_job_failed(job_id, message)


def _resolve_job_description(request: dict[str, Any], job_record: dict[str, Any]) -> str:
    if "job_description" in request:
        return str(request.get("job_description", "General resume analysis"))
    if job_record.get("job_description"):
        return str(job_record["job_description"])
    return "General resume analysis"


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Lambda handler for queuing and executing resume analysis."""
    worker_job_id = _parse_worker_event(event)
    if worker_job_id:
        return _handle_worker_event(event, context)

    request, error = _extract_request(event)
    if error:
        return _error_response(event, 400, error)

    if request is None:
        return _error_response(event, 400, "No body in request")

    validation_error = _validate_analyze_request(request)
    if validation_error:
        return _error_response(event, 400, validation_error)

    job_id = str(request["job_id"]).strip()
    job_record = _get_job_record(job_id)
    return _queue_analysis_request(event, request, job_id, job_record)
