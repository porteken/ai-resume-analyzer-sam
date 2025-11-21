# AI Resume Analyzer - Backend API

Backend for AI resume analyzer using CrewAI and Google Gemini.

## API Endpoints

### POST /upload
Upload a resume and get a job ID back immediately.

```bash
curl -X POST $API_ENDPOINT/upload \
  -H "x-api-key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "pdf_base64": "base64_encoded_pdf",
    "job_description": "Software Engineer position...",
    "filename": "resume.pdf"
  }'
```

**Response (202 Accepted):**
```json
{
  "job_id": "ABC",
  "status": "processing",
  "message": "Resume uploaded successfully. Analysis in progress."
}
```

### GET /status/{job_id}
Check if analysis is complete and get back results.

```bash
curl $API_ENDPOINT/status/JOB_ID \
  -H "x-api-key: $API_KEY"
```

**Response (200 OK):**
```json
{
  "job_id": "ABC",
  "status": "completed",
  "analysis_result": "## Resume Analysis\n\n### Met Requirements\n..."
}
```

