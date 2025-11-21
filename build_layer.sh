#!/bin/bash
set -e

echo "Building Lambda layer with Docker..."

docker run --rm \
  -v "$PWD":/var/task \
  public.ecr.aws/sam/build-python3.11:latest \
  /bin/bash -c "rm -rf /var/task/layers/dependencies/python"

mkdir -p layers/dependencies/python

docker run --rm \
  -v "$PWD":/var/task \
  -u $(id -u):$(id -g) \
  public.ecr.aws/sam/build-python3.11:latest \
  /bin/bash -c "pip install -r requirements.txt -t /var/task/layers/dependencies/python --no-cache-dir"

echo "Cleaning up layer..."
find layers/dependencies/python -type d -name "tests" -exec rm -rf {} + 2>/dev/null || true
find layers/dependencies/python -type d -name "test" -exec rm -rf {} + 2>/dev/null || true
find layers/dependencies/python -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
find layers/dependencies/python -type f -name "*.pyc" -delete 2>/dev/null || true
find layers/dependencies/python -type f -name "*.pyo" -delete 2>/dev/null || true
find layers/dependencies/python -type f -name "*.dist-info/RECORD" -delete 2>/dev/null || true
find layers/dependencies/python -type f -name "*.dist-info/WHEEL" -delete 2>/dev/null || true

echo "Layer size:"
du -sh layers/dependencies/python

echo "Build complete!"
