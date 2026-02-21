# AI Resume Analyzer (SAM)

Backend for resume analysis using AWS Lambda + S3 and Google Gemini.

## Flow
1. `POST /upload` returns an S3 presigned POST for direct PDF upload.
2. Client uploads the PDF directly to S3.
3. `POST /analyze` downloads the S3 PDF and calls Gemini `gemini-3-flash-preview` with JSON-schema structured output.
4. Optional: `GET /status/{job_id}` fetches stored result/status from DynamoDB.

## Sample JSON Schema
Used for Gemini structured output:

```json
{
  "type": "object",
  "properties": {
    "name": {"type": "string"},
    "contact_info": {
      "type": "object",
      "properties": {
        "email": {"type": "string"},
        "phone": {"type": "string"},
        "location": {"type": "string"},
        "linkedin": {"type": "string"}
      },
      "required": ["email", "phone", "location", "linkedin"],
      "additionalProperties": false
    },
    "summary": {"type": "string"},
    "skills": {"type": "array", "items": {"type": "string"}},
    "experience": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "company": {"type": "string"},
          "role": {"type": "string"},
          "duration": {"type": "string"},
          "highlights": {"type": "array", "items": {"type": "string"}}
        },
        "required": ["company", "role", "duration", "highlights"],
        "additionalProperties": false
      }
    },
    "gaps": {"type": "array", "items": {"type": "string"}},
    "recommendations": {"type": "array", "items": {"type": "string"}}
  },
  "required": ["name", "contact_info", "summary", "skills", "experience", "gaps", "recommendations"],
  "additionalProperties": false
}
```

## cURL Examples

### 1) Request presigned upload

```bash
curl -sS -X POST "$API_BASE/upload" \
  -H "Content-Type: application/json" \
  -d '{
    "filename": "resume.pdf",
    "job_description": "Senior Python backend engineer"
  }' > /tmp/presign.json
```

### 2) Upload PDF directly to S3 using presigned fields

```bash
curl -sS -X POST "$(jq -r '.upload.url' /tmp/presign.json)" \
  -F "key=$(jq -r '.upload.fields.key' /tmp/presign.json)" \
  -F "Content-Type=$(jq -r '.upload.fields["Content-Type"]' /tmp/presign.json)" \
  -F "x-amz-meta-job_id=$(jq -r '.upload.fields["x-amz-meta-job_id"]' /tmp/presign.json)" \
  -F "x-amz-meta-filename=$(jq -r '.upload.fields["x-amz-meta-filename"]' /tmp/presign.json)" \
  -F "policy=$(jq -r '.upload.fields.policy' /tmp/presign.json)" \
  -F "x-amz-algorithm=$(jq -r '.upload.fields["x-amz-algorithm"]' /tmp/presign.json)" \
  -F "x-amz-credential=$(jq -r '.upload.fields["x-amz-credential"]' /tmp/presign.json)" \
  -F "x-amz-date=$(jq -r '.upload.fields["x-amz-date"]' /tmp/presign.json)" \
  -F "x-amz-signature=$(jq -r '.upload.fields["x-amz-signature"]' /tmp/presign.json)" \
  -F "file=@resume.pdf;type=application/pdf"
```

### 3) Trigger analysis and verify JSON response

```bash
curl -sS -X POST "$API_BASE/analyze" \
  -H "Content-Type: application/json" \
  -d "{
    \"job_id\": \"$(jq -r '.job_id' /tmp/presign.json)\",
    \"s3_url\": \"$(jq -r '.s3_url' /tmp/presign.json)\",
    \"job_description\": \"Senior Python backend engineer\"
  }" | jq .
```

Expected `analysis_result` is JSON with:
- `name`
- `contact_info`
- `summary`
- `skills[]`
- `experience[]`
- `gaps[]`
- `recommendations[]`
