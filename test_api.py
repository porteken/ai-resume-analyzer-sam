"""Testing script for API."""

import base64
import json
import os
import sys
import time

import requests

API_ENDPOINT = os.environ.get("API_ENDPOINT", sys.argv[1] if len(sys.argv) > 1 else None)
API_KEY = os.environ.get("API_KEY", sys.argv[2] if len(sys.argv) > 2 else None)
PDF_FILE = sys.argv[3] if len(sys.argv) > 3 else "resume.pdf"
JOB_DESCRIPTION = (
    sys.argv[4] if len(sys.argv) > 4 else "Software Engineer with Python and AWS experience"
)

if not API_ENDPOINT or not API_KEY:
    print("Usage: python test_async_api.py <API_ENDPOINT> <API_KEY> [PDF_FILE] [JOB_DESCRIPTION]")
    sys.exit(1)
API_BASE = API_ENDPOINT.rstrip("/").replace("/upload", "").replace("/analyze", "")

if not os.path.exists(PDF_FILE):
    print(f"ERROR: File not found: {PDF_FILE}")
    sys.exit(1)

file_size = os.path.getsize(PDF_FILE)

with open(PDF_FILE, "rb") as f:
    pdf_bytes = f.read()
    pdf_base64 = base64.b64encode(pdf_bytes).decode("utf-8")

base64_size = len(pdf_base64)


upload_payload = {
    "pdf_base64": pdf_base64,
    "job_description": JOB_DESCRIPTION,
    "filename": os.path.basename(PDF_FILE),
}

payload_size = len(json.dumps(upload_payload))


headers = {"x-api-key": API_KEY, "Content-Type": "application/json"}

upload_url = f"{API_BASE}/upload"


upload_start = time.time()

try:
    upload_response = requests.post(upload_url, headers=headers, json=upload_payload, timeout=30)

    upload_duration = time.time() - upload_start

    try:
        upload_data = upload_response.json()

        if upload_response.status_code != 202:
            print(f"ERROR: Expected status 202, got {upload_response.status_code}")
            if "error" in upload_data:
                print(f"Error message: {upload_data['error']}")
            sys.exit(1)

        job_id = upload_data.get("job_id")
        if not job_id:
            print("ERROR: No job_id in response")
            sys.exit(1)

    except json.JSONDecodeError:
        print("ERROR: Response is not valid JSON:")
        print(upload_response.text[:500])
        sys.exit(1)

except requests.exceptions.Timeout:
    print("ERROR: Upload request timed out")
    sys.exit(1)

except requests.exceptions.RequestException as e:
    print(f"ERROR: Upload request failed: {e}")
    sys.exit(1)


status_url = f"{API_BASE}/status/{job_id}"

poll_start = time.time()
poll_count = 0
max_polls = 150

while poll_count < max_polls:
    poll_count += 1
    elapsed = time.time() - poll_start

    try:
        status_response = requests.get(status_url, headers={"x-api-key": API_KEY}, timeout=10)

        if status_response.status_code != 200:
            print(f"ERROR: Status check failed with code {status_response.status_code}")
            print(status_response.text)
            sys.exit(1)

        status_data = status_response.json()
        status = status_data.get("status", "unknown")

        if status == "processing":
            print("Still processing...")
            time.sleep(2)
            continue

        elif status == "completed":
            analysis_result = status_data.get("analysis_result", "")

            if analysis_result:
                print(analysis_result)

            else:
                print("WARNING: No analysis result in response")

            total_duration = time.time() - upload_start

            sys.exit(0)

        elif status == "failed":
            error = status_data.get("error", "Unknown error")
            print(f"Analysis failed: {error}")
            print(json.dumps(status_data, indent=2))
            sys.exit(1)

        else:
            print(f"Unknown status: {status}")
            print(json.dumps(status_data, indent=2))
            sys.exit(1)

    except requests.exceptions.Timeout:
        print("Timeout (retrying...)")
        time.sleep(2)
        continue

    except requests.exceptions.RequestException as e:
        print(f"Request failed: {e}")
        time.sleep(2)
        continue

    except json.JSONDecodeError:
        print("Invalid JSON response")
        print(status_response.text[:200])
        sys.exit(1)

sys.exit(1)
