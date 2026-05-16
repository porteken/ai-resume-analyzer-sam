"""Regression tests for SAM template layer packaging."""

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
