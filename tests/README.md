# AI Resume Analyzer - Test Suite

Comprehensive unit tests for the AI Resume Analyzer Lambda functions using pytest.

## Overview

This test suite provides comprehensive coverage for:
- **upload_handler.py**: PDF upload and job creation
- **status_handler.py**: Job status retrieval
- **lambda_function.py**: Resume analysis and processing

## Test Structure

```
tests/
├── __init__.py
├── conftest.py              # Shared fixtures and pytest configuration
├── test_upload_handler.py   # Upload handler tests (18 tests)
├── test_status_handler.py   # Status handler tests (13 tests)
├── test_lambda_function.py  # Main analyzer tests (40+ tests)
├── requirements.txt         # Test dependencies
└── README.md               # This file
```

## Running Tests

### Install Test Dependencies

```bash
pip install -r tests/requirements.txt
```

### Run All Tests

```bash
pytest
```

### Run Specific Test Files

```bash
# Test upload handler only
pytest tests/test_upload_handler.py

# Test status handler only
pytest tests/test_status_handler.py

# Test main analyzer only
pytest tests/test_lambda_function.py
```

### Run with Coverage Report

```bash
# Terminal report
pytest --cov=resume_analyzer --cov-report=term-missing

# HTML report
pytest --cov=resume_analyzer --cov-report=html
open htmlcov/index.html
```

### Run Specific Tests

```bash
# Run a specific test class
pytest tests/test_upload_handler.py::TestUploadHandler

# Run a specific test
pytest tests/test_upload_handler.py::TestUploadHandler::test_successful_upload

# Run tests matching a pattern
pytest -k "upload"
```

### Run with Verbose Output

```bash
pytest -v
pytest -vv  # Extra verbose
```

## Test Coverage

The test suite aims for >90% code coverage across all Lambda functions:

| Module | Coverage | Tests |
|--------|----------|-------|
| upload_handler.py | ~95% | 18 tests |
| status_handler.py | ~98% | 13 tests |
| lambda_function.py | ~92% | 40+ tests |

## Key Test Scenarios

### Upload Handler Tests
- ✅ Successful PDF upload
- ✅ Missing/invalid request body
- ✅ Invalid base64 encoding
- ✅ Metadata sanitization (newlines)
- ✅ Long metadata truncation
- ✅ S3 and DynamoDB error handling
- ✅ CORS headers
- ✅ Default values for optional fields

### Status Handler Tests
- ✅ Retrieve completed jobs
- ✅ Retrieve processing jobs
- ✅ Retrieve failed jobs
- ✅ Job not found (404)
- ✅ Missing job_id parameter
- ✅ DynamoDB error handling
- ✅ CORS headers
- ✅ Optional fields handling

### Analyzer Tests
- ✅ PDF text extraction from bytes
- ✅ PDF reading from S3
- ✅ Resume analysis with LLM
- ✅ S3 event parsing
- ✅ API event parsing
- ✅ DynamoDB status updates
- ✅ Error handling (PDF errors, LLM errors, AWS errors)
- ✅ Account ID caching
- ✅ CORS headers
- ✅ Complete end-to-end flows

## Fixtures

Shared fixtures in `conftest.py`:

- **mock_env_vars**: Sets up required environment variables
- **mock_boto3_clients**: Mocks AWS SDK clients (S3, DynamoDB, STS)
- **sample_pdf_base64**: Valid PDF file in base64
- **sample_upload_event**: Sample API Gateway upload event
- **sample_status_event**: Sample API Gateway status event
- **sample_s3_event**: Sample S3 trigger event
- **mock_lambda_context**: Mock Lambda context object

## Mocking Strategy

Tests use `unittest.mock` to isolate units under test:

1. **AWS Services**: All boto3 clients are mocked (no real AWS calls)
2. **External APIs**: Gemini LLM is mocked (no real API calls)
3. **Environment**: Environment variables are mocked
4. **Time**: Datetime is NOT mocked to allow real timestamp generation

## Continuous Integration

Tests run automatically on:
- Push to main branch
- Pull requests
- Manual workflow dispatch

CI checks:
- All tests pass
- Code coverage > 80%
- No linting errors

## Best Practices

1. **Isolation**: Each test is independent and doesn't affect others
2. **Mocking**: External dependencies are mocked for speed and reliability
3. **Clarity**: Test names clearly describe what is being tested
4. **Coverage**: Edge cases and error paths are tested
5. **Maintainability**: Shared fixtures reduce code duplication

## Troubleshooting

### Import Errors

If you get import errors, ensure the project root is in Python path:
```bash
export PYTHONPATH="${PYTHONPATH}:$(pwd)"
pytest
```

Or use the pytest.ini configuration (already included).

### Module Not Found

Install the project dependencies:
```bash
pip install -r layers/dependencies/python/requirements.txt
```

### Mock Issues

If mocks aren't working, check:
1. Patches are applied before imports
2. Mock return values match expected types
3. Environment variables are set in fixtures

## Adding New Tests

1. Create test file: `tests/test_<module>.py`
2. Import the module under test
3. Use fixtures from `conftest.py`
4. Follow naming convention: `test_<scenario>`
5. Run and verify: `pytest tests/test_<module>.py -v`

## Example Test

```python
def test_successful_upload(upload_handler_module, sample_upload_event,
                           mock_lambda_context, mock_boto3_clients):
    """Test successful PDF upload and job creation."""
    response = upload_handler_module.lambda_handler(
        sample_upload_event,
        mock_lambda_context
    )

    assert response["statusCode"] == 202
    body = json.loads(response["body"])
    assert "job_id" in body
    assert body["status"] == "processing"
```

## Resources

- [Pytest Documentation](https://docs.pytest.org/)
- [pytest-cov](https://pytest-cov.readthedocs.io/)
- [unittest.mock](https://docs.python.org/3/library/unittest.mock.html)
- [Moto (AWS Mocking)](https://docs.getmoto.org/)
