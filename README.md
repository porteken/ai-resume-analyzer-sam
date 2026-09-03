# AI Resume Analyzer (SAM)

Backend for resume analysis using AWS API Gateway,Lambda,S3, and Google Gemini.

## Flow
1. `POST /upload` returns an S3 presigned POST for direct PDF upload.
2. Client uploads the PDF directly to S3.
3. `POST /analyze` downloads the S3 PDF and calls Gemini `gemini-3-flash-preview` with JSON-schema structured output.
4. Optional: `GET /status/{job_id}` fetches stored result/status from DynamoDB.
