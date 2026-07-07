#!/bin/bash
# Script to run integration tests against local docker-compose services

set -e

echo "Starting services with docker-compose..."
docker-compose -f deployment/docker-compose.yml up -d

echo "Waiting for services to be ready..."
# Wait for services to start up
sleep 5

# Function to check if a service is responding
check_service() {
    local SERVICE_NAME=$1
    local PORT=$2
    local MAX_ATTEMPTS=30
    local ATTEMPT=0
    
    echo -n "Checking $SERVICE_NAME on port $PORT..."
    while [ $ATTEMPT -lt $MAX_ATTEMPTS ]; do
        if curl -s -o /dev/null -w "%{http_code}" "http://localhost:$PORT/ping/alive" | grep -q "200"; then
            echo " ✅ Ready!"
            return 0
        fi
        ATTEMPT=$((ATTEMPT + 1))
        echo -n "."
        sleep 2
    done
    echo " ❌ Failed to connect after $MAX_ATTEMPTS attempts"
    return 1
}

# Check the local service
check_service "simulation-executor" 8082

echo ""
echo "Running integration tests (excluding GCP workflow tests)..."
cd projects/policyengine-apis-integ
uv sync --extra test
uv run pytest tests/ -v -m "not requires_gcp"
cd ../..

echo ""
echo "Stopping services..."
docker-compose -f deployment/docker-compose.yml down

echo "✅ Integration tests completed!"