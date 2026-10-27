FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /uvx /bin/

RUN uv sync --frozen --no-dev
