import base64
import json
import logging
import os
import re
import uuid
from datetime import UTC, datetime
from typing import Any

import boto3

s3_client = boto3.client("s3")
dynamodb: Any = boto3.resource("dynamodb")

RESUME_BUCKET = os.environ.get("RESUME_BUCKET")
RESULTS_TABLE = os.environ.get("RESULTS_TABLE")
ACCOUNT_ID = os.environ.get("AWS_ACCOUNT_ID", "")
CORS_ALLOW_ORIGIN = os.environ.get("CORS_ALLOW_ORIGIN", "*")
UPLOAD_EXPIRES_SECONDS = int(os.environ.get("UPLOAD_EXPIRES_SECONDS", "900"))
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(10 * 1024 * 1024)))

results_table: Any | None = dynamodb.Table(RESULTS_TABLE) if RESULTS_TABLE else None
logger = logging.getLogger(__name__)


PDF_CONTENT_TYPE = "application/pdf"


def _response(status_code: int, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": CORS_ALLOW_ORIGIN,
        },
        "body": json.dumps(payload),
    }


def _sanitize_filename(filename: str) -> str:
    base = os.path.basename(filename).strip() or "resume.pdf"
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", base)
    if not safe.lower().endswith(".pdf"):
        safe += ".pdf"
    return safe[:256]


def lambda_handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Creates an S3 presigned POST for direct PDF upload."""
    logger.info("Upload handler invoked")

    if not RESUME_BUCKET:
        return _response(500, {"error": "RESUME_BUCKET environment variable not configured"})

    if not results_table:
        return _response(500, {"error": "RESULTS_TABLE environment variable not configured"})

    if "body" not in event:
        return _response(400, {"error": "No body in request"})

    try:
        body = event.get("body", "")
        if event.get("isBase64Encoded", False):
            body = base64.b64decode(body).decode("utf-8")
        if isinstance(body, str):
            body = json.loads(body)

        if not isinstance(body, dict):
            return _response(400, {"error": "Invalid JSON body"})

        filename = _sanitize_filename(str(body.get("filename", "resume.pdf")))
        job_description = str(body.get("job_description", "General resume analysis")).strip()

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

        kwargs: dict[str, Any] = {
            "Bucket": RESUME_BUCKET,
            "Key": s3_key,
            "Fields": fields,
            "Conditions": conditions,
            "ExpiresIn": UPLOAD_EXPIRES_SECONDS,
        }
        if ACCOUNT_ID:
            kwargs["ExpectedBucketOwner"] = ACCOUNT_ID

        presigned_post = s3_client.generate_presigned_post(**kwargs)

        now = datetime.now(UTC)
        results_table.put_item(
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

        return _response(
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
        )
    except json.JSONDecodeError:
        return _response(400, {"error": "Invalid JSON format"})
    except Exception as exc:
        logger.exception("Upload handler failed")
        return _response(500, {"error": str(exc), "type": type(exc).__name__})
