#!/bin/bash
set -euo pipefail

SAM_BUILD_IMAGE="${SAM_BUILD_IMAGE:-public.ecr.aws/sam/build-python3.13:latest}"
TASK_DIR="$(pwd -P)"
HOST_UID="$(id -u)"
HOST_GID="$(id -g)"
readonly TASK_DIR HOST_UID HOST_GID

echo "Exporting requirements.txt from uv..."
uv export --no-dev --format requirements-txt --output-file requirements.txt --locked

echo "Building Lambda layer with Docker..."
echo "Using build image: ${SAM_BUILD_IMAGE}"

if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: docker is required to build the Lambda layer" >&2
  exit 1
fi

docker run --rm \
  -v "${TASK_DIR}":/var/task \
  "${SAM_BUILD_IMAGE}" \
  /bin/bash -c "rm -rf /var/task/layers/dependencies/python"

mkdir -p layers/dependencies/python

docker run --rm \
  -v "${TASK_DIR}":/var/task \
  -u "${HOST_UID}:${HOST_GID}" \
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

for forbidden in boto3 botocore; do
  if [ -d "layers/dependencies/python/${forbidden}" ]; then
    echo "ERROR: ${forbidden} is bundled in the layer; it is already provided by the Lambda runtime" >&2
    exit 1
  fi
done

echo "Layer size:"
du -sh layers/dependencies/python

echo "Build complete!"
