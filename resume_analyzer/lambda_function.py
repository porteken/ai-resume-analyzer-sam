import base64
import io
import json
import logging
import os
import warnings
from datetime import datetime
from functools import lru_cache
from typing import Any

import boto3
from botocore.exceptions import ClientError
from PyPDF2 import PdfReader

warnings.filterwarnings("ignore")

logger = logging.getLogger(__name__)

s3_client = boto3.client("s3")
dynamodb: Any = boto3.resource("dynamodb")
sts_client = boto3.client("sts")

api_key = os.environ.get("GOOGLE_API_KEY")
RESUME_BUCKET = os.environ.get("RESUME_BUCKET")
RESULTS_TABLE = os.environ.get("RESULTS_TABLE")

EXPR_ATTR_STATUS = "#status"
EXPR_VAL_STATUS = ":status"
EXPR_VAL_ERROR = ":error"

logger.info("Lambda initialized. API_KEY: %s, BUCKET: %s", bool(api_key), RESUME_BUCKET)

gemini_llm = None
if api_key:
    try:
        from langchain_google_genai import ChatGoogleGenerativeAI
        gemini_llm = ChatGoogleGenerativeAI(
            model="gemini-1.5-flash",
            google_api_key=api_key,
        )
        logger.info("Gemini LLM initialized successfully")
    except Exception:
        logger.exception("ERROR initializing Gemini LLM")


@lru_cache(maxsize=1)
def get_aws_account_id() -> str:
    """Fetches and caches AWS Account ID."""
    try:
        return sts_client.get_caller_identity()["Account"]
    except Exception:
        logger.exception("ERROR fetching Account ID")
        return ""


def read_pdf_from_bytes(pdf_bytes: bytes) -> str:
    """Reads a PDF from bytes and returns its text content."""
    try:
        with io.BytesIO(pdf_bytes) as file:
            pdf_reader = PdfReader(file)
            text = ""
            for page in pdf_reader.pages:
                page_text = page.extract_text()
                if page_text:
                    text += page_text
            if not text:
                return "Error: Could not extract text from the PDF."
            return text
    except Exception as e:
        logger.exception("ERROR reading PDF")
        return f"An error occurred while reading the PDF: {e}"


def read_pdf_from_s3(bucket_name: str, key: str) -> str:
    """Reads a PDF from S3 and returns text."""
    account_id = get_aws_account_id()
    try:
        logger.info("Reading PDF from s3://%s/%s", bucket_name, key)
        kwargs = {"Bucket": bucket_name, "Key": key}
        if account_id:
            kwargs["ExpectedBucketOwner"] = account_id

        s3_object = s3_client.get_object(**kwargs)
        pdf_content = s3_object["Body"].read()
        return read_pdf_from_bytes(pdf_content)
    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchKey":
            return f"Error: The file '{key}' was not found in bucket '{bucket_name}'."
        return f"AWS ClientError (Access/Ownership): {e}"
    except Exception as e:
        logger.exception("An error occurred while reading the PDF from S3")
        return f"An error occurred while reading the PDF from S3: {e}"


def save_pdf_to_s3(pdf_bytes: bytes, filename: str) -> str | None:
    """Saves PDF bytes to S3 and returns the S3 key."""
    if not RESUME_BUCKET:
        logger.error("ERROR: RESUME_BUCKET environment variable not set")
        return None

    account_id = get_aws_account_id()
    try:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        s3_key = f"uploads/{timestamp}_{filename}"

        kwargs = {
            "Bucket": RESUME_BUCKET,
            "Key": s3_key,
            "Body": pdf_bytes,
            "ContentType": "application/pdf",
        }
        if account_id:
            kwargs["ExpectedBucketOwner"] = account_id

        s3_client.put_object(**kwargs)
        return s3_key
    except Exception:
        logger.exception("ERROR saving PDF to S3")
        return None


def _get_s3_metadata(bucket: str, key: str) -> dict:
    """Retrieves metadata for a job from S3."""
    account_id = get_aws_account_id()
    try:
        kwargs = {"Bucket": bucket, "Key": key}
        if account_id:
            kwargs["ExpectedBucketOwner"] = account_id
        return s3_client.head_object(**kwargs).get("Metadata", {})
    except Exception as e:
        logger.warning("WARNING: Could not get S3 metadata: %s", e)
        return {}


def _update_job_status(
    job_id_param: str | None,
    status_value: str,
    result: str | None = None,
    error_msg: str | None = None,
) -> None:
    """Helper function to update job status in DynamoDB."""
    if not job_id_param or not RESULTS_TABLE:
        return

    try:
        table = dynamodb.Table(RESULTS_TABLE)
        update_expression = f"SET {EXPR_ATTR_STATUS} = {EXPR_VAL_STATUS}"
        expression_values = {EXPR_VAL_STATUS: status_value}

        if result:
            update_expression += ", analysis_result = :result, completed_at = :completed"
            expression_values[":result"] = result
            expression_values[":completed"] = datetime.now().isoformat()

        if error_msg:
            update_expression += f", error = {EXPR_VAL_ERROR}"
            expression_values[EXPR_VAL_ERROR] = error_msg

        table.update_item(
            Key={"job_id": job_id_param},
            UpdateExpression=update_expression,
            ExpressionAttributeNames={EXPR_ATTR_STATUS: "status"},
            ExpressionAttributeValues=expression_values,
        )
    except Exception as e:
        logger.warning("WARNING: Failed to update DynamoDB: %s", e)


def analyze_resume(resume_content: str, job_description: str) -> str:
    """Run LLM analysis."""
    if not gemini_llm:
        return "Error: Gemini LLM not initialized. Check API key configuration."

    try:
        prompt = f"""You are an expert recruiter analyzing a resume against a job description.

JOB DESCRIPTION:
{job_description}

RESUME:
{resume_content}

Provide a comprehensive analysis in this exact format:

## Key Strengths
- [Relevant skill/experience 1]
- [Relevant skill/experience 2]
- [Relevant skill/experience 3]
- [Relevant skill/experience 4]
- [Relevant skill/experience 5]

## Gaps & Areas for Development
- [Missing skill/experience 1]
- [Missing skill/experience 2]
- [Missing skill/experience 3]
- [Missing skill/experience 4]
- [Missing skill/experience 5]

## Recommendations
- [Actionable recommendation 1]
- [Actionable recommendation 2]
- [Actionable recommendation 3]

Be specific, concise, and focus on technical qualifications."""

        response = gemini_llm.invoke(prompt)

        if hasattr(response, "content"):
            return str(response.content)
        return str(response)

    except Exception as e:
        logger.exception("Error during analysis")
        return f"Error during analysis: {e}"


def _parse_s3_event(event: dict) -> tuple[dict[str, Any] | None, str | None]:
    """Parses S3 event."""
    try:
        s3_record = event["Records"][0]["s3"]
        bucket = s3_record["bucket"]["name"]
        key = s3_record["object"]["key"]
        metadata = _get_s3_metadata(bucket, key)

        data = {
            "resume_content": read_pdf_from_s3(bucket, key),
            "job_description": metadata.get("job_description", "General analysis"),
            "job_id": metadata.get("job_id"),
            "s3_key": key,
            "s3_bucket": bucket,
        }
        return data, None
    except Exception as e:
        return None, f"Error parsing S3 event: {e}"


def _process_api_body(body_json: dict) -> tuple[dict[str, Any] | None, str | None]:
    """Parses JSON body for API events."""
    data = {
        "job_description": body_json.get("job_description", "General analysis"),
        "job_id": body_json.get("job_id"),
        "s3_key": None,
        "s3_bucket": None,
        "resume_content": None,
    }

    if "pdf_base64" in body_json:
        try:
            pdf_bytes = base64.b64decode(body_json["pdf_base64"])
            filename = body_json.get("filename", "resume.pdf")
            data["s3_key"] = save_pdf_to_s3(pdf_bytes, filename)
            data["s3_bucket"] = RESUME_BUCKET
            data["resume_content"] = read_pdf_from_bytes(pdf_bytes)
        except Exception as e:
            return None, f"Error decoding base64 PDF: {e}"

    elif "s3_bucket" in body_json and "s3_key" in body_json:
        data["s3_bucket"] = body_json["s3_bucket"]
        data["s3_key"] = body_json["s3_key"]
        data["resume_content"] = read_pdf_from_s3(data["s3_bucket"], data["s3_key"])

    else:
        return None, "Request must contain 'pdf_base64' or 's3_bucket' and 's3_key'."

    return data, None


def _extract_event_data(event: dict) -> tuple[dict[str, Any], str | None]:
    """Pattern to extract data from S3 or API events."""
    if "Records" in event and event["Records"] and "s3" in event["Records"][0]:
        data, error = _parse_s3_event(event)
        if error:
            return {}, error
        return data, None  # type: ignore

    if "body" not in event:
        return {}, "No body in request"

    try:
        body = event.get("body", "")
        if event.get("isBase64Encoded", False):
            body = base64.b64decode(body)
        if isinstance(body, bytes):
            body = body.decode("utf-8")

        data, error = _process_api_body(json.loads(body))
        if error:
            return {}, error
        return data, None  # type: ignore
    except json.JSONDecodeError:
        return {}, "Invalid JSON format"
    except Exception as e:
        return {}, f"Unexpected parsing error: {e}"


def lambda_handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Lambda handler for resume analysis."""
    if not api_key:
        return {"statusCode": 500, "body": json.dumps({"error": "API key not configured."})}

    data, parse_error = _extract_event_data(event)
    if parse_error:
        return {"statusCode": 400, "body": json.dumps({"error": parse_error})}

    resume_content = data.get("resume_content")
    job_id = data.get("job_id")

    if not resume_content or "Error:" in resume_content:
        msg = resume_content if resume_content else "Failed to extract content"
        logger.error("ERROR: %s", msg)
        if job_id:
            _update_job_status(job_id, "failed", error_msg=msg)
        return {"statusCode": 500, "body": json.dumps({"error": msg})}

    try:
        logger.info("Starting resume analysis...")
        result = analyze_resume(resume_content, data.get("job_description", ""))

        if job_id:
            _update_job_status(job_id, "completed", result=result)

        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
            "body": json.dumps(
                {
                    "message": "Success",
                    "analysis_result": result,
                    "s3_key": data.get("s3_key"),
                    "job_id": job_id,
                }
            ),
        }
    except Exception as e:
        logger.exception("Unexpected error")
        if job_id:
            _update_job_status(job_id, "failed", error_msg=f"{e}")
        return {"statusCode": 500, "body": json.dumps({"error": f"Unexpected error: {e}"})}
