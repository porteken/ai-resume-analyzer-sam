"""Copyright 2026.

Regression tests for SAM template layer packaging.
"""

from pathlib import Path

import pytest


@pytest.mark.unit
def test_dependencies_layer_uses_prebuilt_python_directory() -> None:
    """Ensure SAM does not rebuild the already-prepared Lambda layer.

    The repository's `build_layer.sh` script writes dependencies into
    `layers/dependencies/python/`, which is already in the structure Lambda
    layers expect. Adding SAM layer build metadata causes the artifact to be
    wrapped again as `python/python/...`, making imports like `google.genai`
    unavailable at runtime.
    """
    template_lines = Path("template.yaml").read_text(encoding="utf-8").splitlines()

    start_index = template_lines.index("  DependenciesLayer:")
    body_lines: list[str] = []
    for line in template_lines[start_index + 1 :]:
        if line.startswith("  ") and not line.startswith("    "):
            break
        body_lines.append(line)

    body = "\n".join(body_lines)
    assert "ContentUri: layers/dependencies/" in body
    assert "BuildMethod:" not in body
    assert "BuildArchitecture:" not in body


@pytest.mark.unit
def test_template_prefers_secrets_manager_for_gemini_key() -> None:
    """Ensure Gemini supports Secrets Manager while retaining a CI-compatible fallback."""
    template = Path("template.yaml").read_text(encoding="utf-8")

    assert "\n  GoogleApiKey:\n" in template
    assert "NoEcho: true" in template
    assert "\n          GOOGLE_API_KEY:" in template
    assert "GOOGLE_API_KEY_SECRET_ARN:" in template
    assert "UseGoogleApiKeySecretArn" in template
    assert "secretsmanager:GetSecretValue" in template


@pytest.mark.unit
def test_template_has_async_worker_and_least_privilege_permissions() -> None:
    """Ensure async invocation and narrowed IAM/API permissions stay in place."""
    template = Path("template.yaml").read_text(encoding="utf-8")

    assert "ANALYZE_FUNCTION_NAME:" in template
    assert "lambda:InvokeFunction" in template
    assert "dynamodb:PutItem" in template
    assert "PresignedUploadFunction" in template
    assert (
        "arn:aws:execute-api:${AWS::Region}:${AWS::AccountId}:${ExistingApiId}/*/*/*"
        not in template
    )
    assert "${ExistingApiId}/${ApiStageName}/POST/upload" in template
    assert "${ExistingApiId}/${ApiStageName}/POST/analyze" in template
    assert "${ExistingApiId}/${ApiStageName}/GET/status/*" in template


@pytest.mark.unit
def test_template_has_non_wildcard_s3_cors_and_operational_alarms() -> None:
    """Ensure CORS and operational monitoring are configured defensibly."""
    template = Path("template.yaml").read_text(encoding="utf-8")

    assert 'AllowedHeaders:\n              - "*"' not in template
    assert "AnalyzeErrorsAlarm:" in template
    assert "AnalyzeThrottlesAlarm:" in template
    assert "AnalyzeDurationAlarm:" in template
    assert "GeminiFailuresAlarm:" in template
    assert "AWS::Logs::MetricFilter" in template
