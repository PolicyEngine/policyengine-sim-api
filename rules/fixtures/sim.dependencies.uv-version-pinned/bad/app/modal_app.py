"""A Modal image that installs uv without naming its release.

Without uv_version, Modal copies uv from the uv image's latest tag, as the
executor and gateway images did before uv was pinned.
"""

import modal

image = modal.Image.debian_slim(python_version="3.13").uv_sync(
    uv_project_dir=".",
    frozen=True,
)
