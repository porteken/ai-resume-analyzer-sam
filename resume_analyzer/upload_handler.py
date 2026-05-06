import base64
import json
import logging
import os
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    from resume_analyzer.utils import (
        RESUME_BUCKET,
        api_response,
        get_results_table,
        get_s3_client,
    )
except ImportError:
    from utils import (
        RESUME_BUCKET,
        api_response,
        get_results_table,
        get_s3_client,
    )

UPLOAD_EXPIRES_SECONDS = int(os.environ.get("UPLOAD_EXPIRES_SECONDS", "900"))
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(10 * 1024 * 1024)))
MAX_JOB_DESCRIPTION_LENGTH = 5000

logger = logging.getLogger(__name__)


PDF_CONTENT_TYPE = "application/pdf"


def _sanitize_filename(filename: str) -> str:
    base = Path(filename).name.strip() or "resume.pdf"
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", base)
    if not safe.lower().endswith(".pdf"):
        safe += ".pdf"
    return safe[:256]


def lambda_handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Creates an S3 presigned POST for direct PDF upload."""
    logger.info("Upload handler invoked")

    if not RESUME_BUCKET:
        return api_response(
            500,
            {"error": "RESUME_BUCKET environment variable not configured"},
            event=event,
        )

    try:
        table = get_results_table()

        body = event.get("body")
        if body is None:
            return api_response(400, {"error": "No body in request"}, event=event)

        if event.get("isBase64Encoded", False):
            body = base64.b64decode(body).decode("utf-8")
        if isinstance(body, str):
            body = json.loads(body)

        if not isinstance(body, dict):
            return api_response(400, {"error": "Invalid JSON body"}, event=event)

        filename = _sanitize_filename(str(body.get("filename", "resume.pdf")))
        job_description = str(body.get("job_description", "General resume analysis")).strip()[
            :MAX_JOB_DESCRIPTION_LENGTH
        ]

        job_id = str(uuid.uuid4())
        s3_key = f"uploads/{job_id}/{filename}"

        fields = {
            "Content-Type": PDF_CONTENT_TYPE,
            "x-amz-meta-job_id": job_id,
            "x-amz-meta-filename": filename,
        }
        conditions: list[Any] = [
            {"Content-Type": PDF_CONTENT_TYPE},
            {"x-amz-meta-job_id": job_id},
            {"x-amz-meta-filename": filename},
            ["content-length-range", 1, MAX_UPLOAD_BYTES],
        ]

        presigned_post = get_s3_client().generate_presigned_post(
            Bucket=RESUME_BUCKET,
            Key=s3_key,
            Fields=fields,
            Conditions=conditions,
            ExpiresIn=UPLOAD_EXPIRES_SECONDS,
        )

        now = datetime.now(UTC)
        table.put_item(
            Item={
                "job_id": job_id,
                "status": "upload_pending",
                "s3_key": s3_key,
                "s3_bucket": RESUME_BUCKET,
                "filename": filename,
                "job_description": job_description,
                "created_at": now.isoformat().replace("+00:00", "Z"),
                "ttl": int(now.timestamp()) + 86400,
            }
        )

        return api_response(
            200,
            {
                "job_id": job_id,
                "status": "upload_pending",
                "s3_key": s3_key,
                "s3_url": f"s3://{RESUME_BUCKET}/{s3_key}",
                "expires_in": UPLOAD_EXPIRES_SECONDS,
                "upload": {
                    "url": presigned_post["url"],
                    "fields": presigned_post["fields"],
                },
            },
            event=event,
        )
    except json.JSONDecodeError:
        return api_response(400, {"error": "Invalid JSON format"}, event=event)
    except (TypeError, ValueError) as exc:
        return api_response(400, {"error": str(exc)}, event=event)
    except RuntimeError as exc:
        return api_response(500, {"error": str(exc)}, event=event)
    except Exception:
        logger.exception("Upload handler failed")
        return api_response(500, {"error": "Internal server error"}, event=event)
