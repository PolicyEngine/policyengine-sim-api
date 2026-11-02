"""A Modal image that installs the uv release in .uv-version."""

import modal

image = modal.Image.debian_slim(python_version="3.13").uv_sync(
    uv_project_dir=".",
    frozen=True,
    uv_version="0.13.6",
)
