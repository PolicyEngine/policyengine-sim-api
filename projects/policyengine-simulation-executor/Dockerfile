# Multi-stage build inheriting from base
# Use explicit platform for consistency across builds
FROM --platform=$BUILDPLATFORM python:3.13-slim AS base

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Set common environment variables
ENV ENVIRONMENT="production"

WORKDIR /app

# Copy shared libraries and LICENSE
COPY LICENSE ./LICENSE
COPY libs ./libs/

# Final stage
FROM base AS final

# Service-specific environment
ENV OT_SERVICE_NAME=policyengine_simulation_api \
    OT_SERVICE_INSTANCE_ID=instance

# Copy service-specific code
COPY projects/policyengine-simulation-executor ./projects/policyengine-simulation-executor/

WORKDIR /app/projects/policyengine-simulation-executor

# Install dependencies
RUN uv sync --frozen --no-dev

EXPOSE 8080

# Run with uvicorn (with 2 workers for simulation API)
CMD ["uv", "run", "uvicorn", "policyengine_simulation_executor.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "2"]