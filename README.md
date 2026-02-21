# AI Resume Analyzer - Backend API

Backend for AI resume analyzer hosted [here](https://ai-resume-analyzer-app-lake.vercel.app/) using CrewAI and Google Gemini.

## GitHub Actions CI/CD (SAM)

This repo includes `.github/workflows/sam-deploy.yml` to build and deploy the SAM stack.

### Required GitHub Secrets

- `AWS_ROLE_TO_ASSUME`: IAM role ARN trusted for GitHub OIDC and authorized to deploy the stack.
- `GOOGLE_API_KEY`: Optional. Used for first-time stack creation or parameter updates. If omitted, deploy uses the existing CloudFormation parameter value.

### Optional GitHub Variables

- `AWS_REGION` (default: `us-east-1`)
- `SAM_STACK_NAME_DEV` (default: `ai-resume-analyzer-dev`)
- `SAM_STACK_NAME_PROD` (default: `ai-resume-analyzer-prod`)

### Trigger behavior

- Pull requests: build/validate only.
- Push to `dev`: build + deploy to dev stack with API stage `dev`.
- Push to `prd`: build + deploy to prod stack with API stage `prod`.
- Manual run (`workflow_dispatch`): build + deploy using the selected branch (`dev` or `prd`).

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
