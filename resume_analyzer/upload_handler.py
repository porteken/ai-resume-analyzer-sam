"""Copyright 2026.

Create presigned upload requests for resume PDFs.
"""

import base64
import binascii
import importlib
import json
import logging
import os
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

try:
    _utils = importlib.import_module("resume_analyzer.utils")
except ImportError:
    _utils = importlib.import_module("utils")

RESUME_BUCKET = _utils.RESUME_BUCKET
MAX_JOB_DESCRIPTION_LENGTH = _utils.MAX_JOB_DESCRIPTION_LENGTH
api_response = _utils.api_response
get_results_table = _utils.get_results_table
get_s3_client = _utils.get_s3_client

UPLOAD_EXPIRES_SECONDS = int(os.environ.get("UPLOAD_EXPIRES_SECONDS", "900"))
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(10 * 1024 * 1024)))

logger = logging.getLogger(__name__)


PDF_CONTENT_TYPE = "application/pdf"
UPLOAD_PENDING_STATUS = "upload_pending"
JOB_TTL_SECONDS = 86400


def _sanitize_filename(filename: str) -> str:
    base = Path(filename).name.strip() or "resume.pdf"
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", base)
    if not safe.lower().endswith(".pdf"):
        safe += ".pdf"
    return safe[:256]


def _error_response(event: dict[str, Any], status_code: int, message: str) -> dict[str, Any]:
    return api_response(status_code, {"error": message}, event=event)


def _extract_request_body(event: dict[str, Any]) -> dict[str, Any] | None:
    raw_body = event.get("body")
    if raw_body is None:
        return None

    body: Any = raw_body
    if event.get("isBase64Encoded", False):
        if not isinstance(raw_body, str | bytes | bytearray):
            raise TypeError("Invalid JSON body")
        body = base64.b64decode(raw_body, validate=True).decode("utf-8")

    parsed = json.loads(body) if isinstance(body, str | bytes | bytearray) else body
    if not isinstance(parsed, dict):
        raise TypeError("Invalid JSON body")
    return parsed


def _upload_metadata(body: dict[str, Any]) -> tuple[str, str]:
    filename = _sanitize_filename(str(body.get("filename", "resume.pdf")))
    job_description = str(body.get("job_description", "General resume analysis")).strip()
    return filename, job_description[:MAX_JOB_DESCRIPTION_LENGTH]


def _upload_fields(job_id: str, filename: str) -> dict[str, str]:
    return {
        "Content-Type": PDF_CONTENT_TYPE,
        "x-amz-meta-job_id": job_id,
        "x-amz-meta-filename": filename,
    }


def _upload_conditions(fields: dict[str, str]) -> list[Any]:
    return [{field: value} for field, value in fields.items()] + [
        ["content-length-range", 1, MAX_UPLOAD_BYTES]
    ]


def _create_presigned_upload(job_id: str, filename: str) -> tuple[str, dict[str, Any]]:
    s3_key = f"uploads/{job_id}/{filename}"
    fields = _upload_fields(job_id, filename)
    return s3_key, get_s3_client().generate_presigned_post(
        Bucket=RESUME_BUCKET,
        Key=s3_key,
        Fields=fields,
        Conditions=_upload_conditions(fields),
        ExpiresIn=UPLOAD_EXPIRES_SECONDS,
    )


def _store_upload_job(
    table: Any,
    *,
    job_id: str,
    s3_key: str,
    filename: str,
    job_description: str,
) -> None:
    now = datetime.now(UTC)
    table.put_item(
        Item={
            "job_id": job_id,
            "status": UPLOAD_PENDING_STATUS,
            "s3_key": s3_key,
            "s3_bucket": RESUME_BUCKET,
            "filename": filename,
            "job_description": job_description,
            "created_at": now.isoformat().replace("+00:00", "Z"),
            "ttl": int(now.timestamp()) + JOB_TTL_SECONDS,
        }
    )


def _upload_response(
    event: dict[str, Any],
    *,
    job_id: str,
    s3_key: str,
    presigned_post: dict[str, Any],
) -> dict[str, Any]:
    payload = {
        "job_id": job_id,
        "status": UPLOAD_PENDING_STATUS,
        "s3_key": s3_key,
        "s3_url": f"s3://{RESUME_BUCKET}/{s3_key}",
        "expires_in": UPLOAD_EXPIRES_SECONDS,
        "upload": {
            "url": presigned_post["url"],
            "fields": presigned_post["fields"],
        },
    }
    return api_response(200, payload, event=event)


def lambda_handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Create an S3 presigned POST for direct PDF upload."""
    logger.info("Upload handler invoked")

    if not RESUME_BUCKET:
        return _error_response(event, 500, "RESUME_BUCKET environment variable not configured")

    try:
        table = get_results_table()

        body = _extract_request_body(event)
        if body is None:
            return _error_response(event, 400, "No body in request")

        job_id = str(uuid.uuid4())
        filename, job_description = _upload_metadata(body)
        s3_key, presigned_post = _create_presigned_upload(job_id, filename)
        _store_upload_job(
            table,
            job_id=job_id,
            s3_key=s3_key,
            filename=filename,
            job_description=job_description,
        )
        return _upload_response(event, job_id=job_id, s3_key=s3_key, presigned_post=presigned_post)
    except binascii.Error, UnicodeDecodeError:
        return _error_response(event, 400, "Invalid base64-encoded body")
    except json.JSONDecodeError:
        return _error_response(event, 400, "Invalid JSON format")
    except (TypeError, ValueError) as exc:
        return _error_response(event, 400, str(exc))
    except RuntimeError as exc:
        return _error_response(event, 500, str(exc))
    except BotoCoreError, ClientError:
        logger.exception("Upload handler failed")
        return _error_response(event, 500, "Internal server error")
    except AttributeError, KeyError:
        logger.exception("Unexpected upload handler failure")
        return _error_response(event, 500, "Internal server error")
