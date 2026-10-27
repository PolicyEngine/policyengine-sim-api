FROM python:3.13-slim

# The executor and entrypoint images before uv was pinned: the latest tag
# copies whichever uv release is newest when the image is built.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

RUN uv sync --frozen --no-dev
