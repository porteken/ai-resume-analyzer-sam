#!/bin/bash
set -euo pipefail

SAM_BUILD_IMAGE="${SAM_BUILD_IMAGE:-public.ecr.aws/sam/build-python3.11:1.136.0}"

echo "Building Lambda layer with Docker..."
echo "Using build image: ${SAM_BUILD_IMAGE}"

if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: docker is required to build the Lambda layer" >&2
  exit 1
fi

docker run --rm \
  -v "$PWD":/var/task \
  "${SAM_BUILD_IMAGE}" \
  /bin/bash -c "rm -rf /var/task/layers/dependencies/python"

mkdir -p layers/dependencies/python

docker run --rm \
  -v "$PWD":/var/task \
  -u "$(id -u):$(id -g)" \
  "${SAM_BUILD_IMAGE}" \
  /bin/bash -c "pip install -r requirements.txt -t /var/task/layers/dependencies/python --no-cache-dir"

echo "Cleaning up layer..."
find layers/dependencies/python -type d -name "tests" -exec rm -rf {} + 2>/dev/null || true
find layers/dependencies/python -type d -name "test" -exec rm -rf {} + 2>/dev/null || true
find layers/dependencies/python -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
find layers/dependencies/python -type f -name "*.pyc" -delete 2>/dev/null || true
find layers/dependencies/python -type f -name "*.pyo" -delete 2>/dev/null || true
find layers/dependencies/python -type f -path "*/*.dist-info/RECORD" -delete 2>/dev/null || true
find layers/dependencies/python -type f -path "*/*.dist-info/WHEEL" -delete 2>/dev/null || true

echo "Layer size:"
du -sh layers/dependencies/python

echo "Build complete!"
