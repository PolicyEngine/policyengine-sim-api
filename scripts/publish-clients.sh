#!/bin/bash
# Script to build and publish API client packages to PyPI

set -e

echo "Publishing API client packages to PyPI..."

# Function to publish a client package
publish_client() {
    local SERVICE=$1
    local CLIENT_DIR="projects/policyengine-simulation-gateway/artifacts/clients/python"
    
    echo "Publishing ${SERVICE} API client..."
    
    # Check if client exists
    if [ ! -d "${CLIENT_DIR}" ]; then
        echo "  ❌ Client directory not found: ${CLIENT_DIR}"
        echo "  Running client generation first..."
        ./scripts/generate-clients.sh
    fi
    
    cd "${CLIENT_DIR}"
    
    # Update version based on date and commit SHA
    # Format: 0.YYYYMMDD.SHORT_SHA (e.g., 0.20240820.abc1234)
    if [ -n "${GITHUB_SHA}" ]; then
        # In GitHub Actions
        SHORT_SHA=$(echo "${GITHUB_SHA}" | cut -c1-7)
    else
        # Local development
        SHORT_SHA=$(git rev-parse --short HEAD 2>/dev/null || echo "dev")
    fi
    DATE=$(date +%Y%m%d)
    NEW_VERSION="0.${DATE}.${SHORT_SHA}"
    
    # Update version in pyproject.toml
    echo "  Updating version to: ${NEW_VERSION}"
    sed -i.bak "s/^version = .*/version = \"${NEW_VERSION}\"/" pyproject.toml
    rm pyproject.toml.bak
    
    # Build the package
    echo "  Building package..."
    uv build
    
    # Publish to PyPI
    echo "  Publishing to PyPI..."
    uv publish --token "${PYPI_TOKEN}"
    
    echo "  ✅ Successfully published policyengine_api_${SERVICE//-/_}_client version ${NEW_VERSION}"
    
    cd ../../../../../
}

# Check if PYPI_TOKEN is set
if [ -z "${PYPI_TOKEN}" ]; then
    echo "❌ PYPI_TOKEN environment variable is not set"
    exit 1
fi

# Publish both clients
publish_client "full"
publish_client "simulation"

echo "✅ All clients published successfully!"