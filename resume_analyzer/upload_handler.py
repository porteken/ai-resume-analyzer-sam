import base64
import json
import logging
import os
import uuid
from datetime import datetime
from functools import lru_cache
from typing import Any

import boto3

s3_client = boto3.client("s3")
dynamodb = boto3.resource("dynamodb")
sts_client = boto3.client("sts")

RESUME_BUCKET = os.environ.get("RESUME_BUCKET")
RESULTS_TABLE = os.environ.get("RESULTS_TABLE")

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def get_aws_account_id() -> str:
    """Fetches and caches the AWS Account ID."""
    try:
        return sts_client.get_caller_identity()["Account"]
    except Exception:
        logger.exception("ERROR fetching Account ID")
        return ""


def lambda_handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Upload handler that accepts a PDF, saves to S3, and returns a job ID."""
    logger.info("Upload handler invoked: %s", json.dumps(event, default=str)[:500])

    try:
        if "body" not in event:
            return {
                "statusCode": 400,
                "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
                "body": json.dumps({"error": "No body in request"}),
            }

        body = event.get("body", "")
        is_base64 = event.get("isBase64Encoded", False)

        if is_base64:
            body = base64.b64decode(body)

        if isinstance(body, bytes):
            body = body.decode("utf-8")

        body_json = json.loads(body)

        if "pdf_base64" not in body_json:
            return {
                "statusCode": 400,
                "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
                "body": json.dumps({"error": "Missing required field: pdf_base64"}),
            }

        job_description = body_json.get("job_description", "General resume analysis")
        filename = body_json.get("filename", "resume.pdf")

        pdf_bytes = base64.b64decode(body_json["pdf_base64"])

        job_id = str(uuid.uuid4())
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        s3_key = f"uploads/{timestamp}_{job_id}_{filename}"

        safe_job_description = job_description.replace("\n", " ").replace("\r", " ").strip()
        safe_filename = filename.replace("\n", " ").replace("\r", " ").strip()

        account_id = get_aws_account_id()
        kwargs = {
            "Bucket": RESUME_BUCKET,
            "Key": s3_key,
            "Body": pdf_bytes,
            "ContentType": "application/pdf",
            "Metadata": {
                "job_id": job_id,
                "job_description": safe_job_description[:2000],
                "filename": safe_filename[:256],
            },
        }
        if account_id:
            kwargs["ExpectedBucketOwner"] = account_id

        s3_client.put_object(**kwargs)

        table = dynamodb.Table(RESULTS_TABLE)  # type: ignore[attr-defined]
        table.put_item(
            Item={
                "job_id": job_id,
                "status": "processing",
                "s3_key": s3_key,
                "s3_bucket": RESUME_BUCKET,
                "job_description": job_description,
                "filename": filename,
                "created_at": datetime.now().isoformat(),
                "ttl": int(datetime.now().timestamp()) + 86400,
            }
        )

        logger.info("Created job %s, saved to %s", job_id, s3_key)

        return {
            "statusCode": 202,
            "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
            "body": json.dumps(
                {
                    "job_id": job_id,
                    "status": "processing",
                    "message": "Resume uploaded successfully. Analysis in progress.",
                    "poll_url": f"/status/{job_id}",
                }
            ),
        }

    except json.JSONDecodeError as e:
        logger.exception("JSON decode error")
        return {
            "statusCode": 400,
            "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
            "body": json.dumps({"error": f"Invalid JSON: {e!s}"}),
        }

    except Exception as e:
        logger.exception("ERROR")
        return {
            "statusCode": 500,
            "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
            "body": json.dumps({"error": str(e), "type": type(e).__name__}),
        }
