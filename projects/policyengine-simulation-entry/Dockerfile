FROM --platform=$BUILDPLATFORM python:3.13-slim AS runtime

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PORT=8080

WORKDIR /app

COPY LICENSE ./LICENSE
COPY libs ./libs/
COPY projects/policyengine-simulation-entry ./projects/policyengine-simulation-entry/

WORKDIR /app/projects/policyengine-simulation-entry
RUN uv sync --frozen --no-dev --no-editable

RUN addgroup --system simulation-entry \
    && adduser --system --ingroup simulation-entry simulation-entry

USER simulation-entry

EXPOSE 8080

CMD ["/opt/venv/bin/uvicorn", "policyengine_simulation_entry.app:app", "--host", "0.0.0.0", "--port", "8080"]
