#!/bin/bash
set -e

echo "=== Setting up Python Virtual Environment for Local Testing ==="
echo

# Check Python version
if ! command -v python3.11 &> /dev/null && ! command -v python3 &> /dev/null; then
    echo "❌ ERROR: python3 not found. Please install Python 3.11."
    exit 1
fi

# Prefer python3.11 if available
if command -v python3.11 &> /dev/null; then
    PYTHON_CMD="python3.11"
else
    PYTHON_CMD="python3"
fi

PYTHON_VERSION=$($PYTHON_CMD --version | cut -d' ' -f2 | cut -d'.' -f1-2)
echo "✓ Found Python $PYTHON_VERSION"

# Check if Python version is 3.11
if [[ "$PYTHON_VERSION" != "3.11" ]]; then
    echo "⚠️  WARNING: Python 3.11 is recommended (found $PYTHON_VERSION)"
    echo "   Lambda uses Python 3.11, so local testing should match"
    echo "   Continue anyway? (y/n)"
    read -r response
    if [[ "$response" != "y" ]]; then
        exit 1
    fi
fi

# Create virtual environment
if [ -d "venv" ]; then
    echo "⚠️  Virtual environment already exists. Remove it? (y/n)"
    read -r response
    if [[ "$response" == "y" ]]; then
        rm -rf venv
        echo "✓ Removed old virtual environment"
    else
        echo "Using existing virtual environment"
    fi
fi

if [ ! -d "venv" ]; then
    echo "Creating virtual environment with $PYTHON_CMD..."
    $PYTHON_CMD -m venv venv
    echo "✓ Virtual environment created"
fi

# Activate virtual environment
echo
echo "Activating virtual environment..."
source venv/bin/activate

# Upgrade pip
echo "Upgrading pip..."
pip install --upgrade pip --quiet

# Install development dependencies (not the full Lambda layer)
echo
echo "Installing development dependencies..."
if [ -f "requirements-dev.txt" ]; then
    pip install -r requirements-dev.txt
    echo "✓ Development dependencies installed"
else
    echo "⚠️  requirements-dev.txt not found, installing basics..."
    pip install --quiet boto3 pytest black flake8 pylint python-dotenv ipython
    echo "✓ Basic dependencies installed"
fi

echo
echo "ℹ️  Note: Full Lambda dependencies (CrewAI, etc.) are in the Lambda layer"
echo "   Use 'sam local invoke' to test with all dependencies"

# Create .env template if it doesn't exist
if [ ! -f ".env" ]; then
    cat > .env << 'EOF'
# Lambda Environment Variables for Local Testing
GOOGLE_API_KEY=your-google-api-key-here
RESUME_BUCKET=test-bucket
RESULTS_TABLE=test-results-table
AWS_REGION=us-east-1

# For testing
AWS_ACCESS_KEY_ID=test
AWS_SECRET_ACCESS_KEY=test
AWS_DEFAULT_REGION=us-east-1
EOF
    echo "✓ Created .env template"
    echo "⚠️  Please edit .env and add your Google API key"
fi

echo
echo "=== Setup Complete! ==="
echo
echo "To activate the virtual environment, run:"
echo "  source venv/bin/activate"
echo
echo "To deactivate, run:"
echo "  deactivate"
echo
echo "To run linters:"
echo "  black resume_analyzer/"
echo "  flake8 resume_analyzer/"
echo "  pylint resume_analyzer/"
echo
echo "To run tests locally:"
echo "  python -m pytest"
