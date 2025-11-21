# AI Resume Analyzer - Async API

Production-ready serverless resume analyzer using CrewAI and Google Gemini, designed for React applications with async job processing to avoid API Gateway timeouts.

## 🎯 Features

- ✅ **Async Processing** - No API Gateway timeouts (30s limit bypassed)
- ✅ **AI-Powered Analysis** - Uses CrewAI with Google Gemini for intelligent resume evaluation
- ✅ **Serverless Architecture** - Auto-scales, pay only for what you use
- ✅ **React-Ready** - Designed for modern frontend applications
- ✅ **Production Quality** - Error handling, monitoring, auto-cleanup
- ✅ **Cost-Effective** - ~$2/month for 1,000 analyses

## 📋 Quick Links

- **[API Documentation](README_ASYNC_API.md)** - React integration examples
- **[Testing Guide](TESTING.md)** - How to test the API
- **[Deployment Guide](DEPLOYMENT_GUIDE.md)** - Step-by-step deployment
- **[Solution Summary](SOLUTION_SUMMARY.md)** - Architecture and design decisions

## 🚀 Quick Start

### 1. Deploy

```bash
# Build Lambda layer with dependencies
./build_layer.sh

# Deploy to AWS
sam build
sam deploy --guided --parameter-overrides GoogleApiKey=YOUR_GOOGLE_API_KEY
```

### 2. Test

```bash
# Get API endpoint and key
export API_ENDPOINT=$(aws cloudformation describe-stacks \
  --stack-name ai-resume-analyzer \
  --query 'Stacks[0].Outputs[?OutputKey==`UploadEndpoint`].OutputValue' \
  --output text | sed 's:/upload$::')

export API_KEY=$(aws apigateway get-api-key \
  --api-key $(aws cloudformation describe-stacks \
    --stack-name ai-resume-analyzer \
    --query 'Stacks[0].Outputs[?OutputKey==`ApiKeyId`].OutputValue' \
    --output text) \
  --include-value --query 'value' --output text)

# Run test
./test_async.sh $API_ENDPOINT $API_KEY resume.pdf
```

### 3. Integrate with React

```typescript
// Upload resume
const { job_id } = await fetch(`${API_BASE}/upload`, {
  method: 'POST',
  headers: { 'x-api-key': API_KEY, 'Content-Type': 'application/json' },
  body: JSON.stringify({ pdf_base64, job_description, filename })
}).then(r => r.json());

// Poll for results
const interval = setInterval(async () => {
  const { status, analysis_result } = await fetch(
    `${API_BASE}/status/${job_id}`,
    { headers: { 'x-api-key': API_KEY } }
  ).then(r => r.json());

  if (status === 'completed') {
    clearInterval(interval);
    showResults(analysis_result);
  }
}, 2000);
```

See [README_ASYNC_API.md](README_ASYNC_API.md) for complete React examples with React Query.

## 🏗️ Architecture

```
React App → POST /upload → Upload Lambda → S3 + DynamoDB
                                             ↓ S3 Trigger
                                        Analyzer Lambda
                                        (runs 1-2 minutes)
                                             ↓
                                        DynamoDB (results)
                                             ↑
React App ← GET /status/{job_id} ← Status Lambda (polling)
```

### Components

| Component | Purpose | Timeout | Memory |
|-----------|---------|---------|--------|
| **Upload Lambda** | Accept PDF, save to S3, create job | 30s | 512MB |
| **Analyzer Lambda** | Run CrewAI analysis (background) | 300s | 1024MB |
| **Status Lambda** | Return job status/results | 10s | 256MB |
| **DynamoDB** | Store job status and results | - | - |
| **S3 Bucket** | Store uploaded PDFs | - | - |

## 📊 API Endpoints

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
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "processing",
  "message": "Resume uploaded successfully. Analysis in progress."
}
```

### GET /status/{job_id}

Check if analysis is complete.

```bash
curl $API_ENDPOINT/status/JOB_ID \
  -H "x-api-key: $API_KEY"
```

**Response (200 OK):**
```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "completed",
  "analysis_result": "## Resume Analysis\n\n### Met Requirements\n..."
}
```

## 🧪 Testing

Three test scripts are provided:

```bash
# Full integration test (upload + poll for results)
./test_async.sh $API_ENDPOINT $API_KEY resume.pdf

# Quick upload test only
python3 quick_test.py $API_ENDPOINT $API_KEY resume.pdf

# Python script with detailed output
python3 test_async_api.py $API_ENDPOINT $API_KEY resume.pdf
```

See [TESTING.md](TESTING.md) for complete testing guide.

## 📁 Project Structure

```
ai-resume-analyzer-sam/
├── resume_analyzer/
│   ├── lambda_function.py      # Main analyzer Lambda
│   ├── upload_handler.py       # Upload endpoint
│   └── status_handler.py       # Status endpoint
├── layers/
│   └── dependencies/
│       └── python/             # Python packages (174MB)
├── template.yaml               # CloudFormation/SAM template
├── requirements.txt            # Python dependencies
├── build_layer.sh             # Layer builder script
├── test_async_api.py          # Full integration test
├── quick_test.py              # Quick upload test
├── test_async.sh              # Shell wrapper
├── README.md                  # This file
├── README_ASYNC_API.md        # React integration guide
├── DEPLOYMENT_GUIDE.md        # Deployment instructions
├── SOLUTION_SUMMARY.md        # Architecture overview
└── TESTING.md                 # Testing guide
```

## 💰 Cost Estimate

For **1,000 resume analyses per month**:

| Service | Cost |
|---------|------|
| Lambda (Upload) | < $0.01 |
| Lambda (Status) | ~$0.10 |
| Lambda (Analyzer) | ~$1.50 |
| DynamoDB | ~$0.40 |
| S3 | ~$0.05 |
| **Total** | **~$2.06/month** |

- **First 1 million Lambda requests/month**: Free tier
- **DynamoDB**: 25GB free tier
- **S3**: 5GB free tier

## 🔒 Security

- ✅ API key required for all endpoints
- ✅ CORS configured for your domain
- ✅ S3 bucket is private (no public access)
- ✅ IAM roles follow least privilege
- ✅ CloudWatch logging enabled
- ✅ DynamoDB encryption at rest

## 📈 Monitoring

### View Logs

```bash
# Upload Lambda
sam logs -n UploadFunction --tail

# Analyzer Lambda
sam logs -n ResumeAnalyzerFunction --tail

# Status Lambda
sam logs -n StatusFunction --tail
```

### Check DynamoDB

```bash
# List all jobs
aws dynamodb scan --table-name resume-analyzer-results \
  --query 'Items[*].[job_id.S, status.S]' --output table
```

### CloudWatch Metrics

- Lambda invocations, errors, duration
- DynamoDB read/write capacity
- API Gateway requests, latency

## 🛠️ Development

### Update Dependencies

```bash
# Edit requirements.txt
vim requirements.txt

# Rebuild layer
./build_layer.sh

# Deploy
sam build && sam deploy
```

### Update Lambda Code

```bash
# Edit Lambda functions
vim resume_analyzer/lambda_function.py

# Deploy (no layer rebuild needed)
sam build && sam deploy --no-confirm-changeset
```

### Update Infrastructure

```bash
# Edit template
vim template.yaml

# Deploy with review
sam build && sam deploy
```

## 🐛 Troubleshooting

### CrewAI Import Error

**Issue:** `CrewAI library not available`

**Solution:**
```bash
./build_layer.sh  # Rebuild with Docker
sam build && sam deploy --no-confirm-changeset
```

### API Gateway Timeout (504)

**This is expected for `/analyze` endpoint!** Use `/upload` instead.

### Job Never Completes

**Check CloudWatch logs:**
```bash
sam logs -n ResumeAnalyzerFunction --tail
```

**Common issues:**
- Invalid Google API key
- PDF parsing error
- Out of memory (increase Lambda memory)

See [DEPLOYMENT_GUIDE.md](DEPLOYMENT_GUIDE.md#troubleshooting) for more.

## 🚀 Production Checklist

- [ ] API key in environment variables (not in code)
- [ ] CloudWatch alarms configured
- [ ] DynamoDB TTL enabled (24h auto-cleanup)
- [ ] S3 lifecycle policy set
- [ ] CORS configured for your domain
- [ ] Cost alerts configured
- [ ] Monitoring dashboard created
- [ ] Error tracking integrated (Sentry, etc.)

## 📚 Documentation

- **[README_ASYNC_API.md](README_ASYNC_API.md)** - Complete API guide with React examples
- **[TESTING.md](TESTING.md)** - How to test the API
- **[DEPLOYMENT_GUIDE.md](DEPLOYMENT_GUIDE.md)** - Deployment and troubleshooting
- **[SOLUTION_SUMMARY.md](SOLUTION_SUMMARY.md)** - Architecture decisions

## 🤝 Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Test thoroughly
5. Submit a pull request

## 📝 License

MIT License - feel free to use in your projects

## 🙏 Credits

- **CrewAI** - AI agent orchestration
- **Google Gemini** - LLM for analysis
- **AWS SAM** - Serverless deployment
- **LangChain** - LLM framework

## 💡 Future Enhancements

- [ ] WebSocket support for real-time updates (eliminate polling)
- [ ] Email notifications when analysis completes
- [ ] PDF format validation
- [ ] Rate limiting per user
- [ ] Multiple LLM support (OpenAI, Anthropic)
- [ ] Resume comparison feature
- [ ] Custom analysis templates
- [ ] Batch processing

## 📞 Support

- **Issues:** Open a GitHub issue
- **Questions:** Check existing documentation
- **AWS Costs:** Monitor CloudWatch and billing dashboard

---

**Built with ❤️ for developers who hate API timeouts**
