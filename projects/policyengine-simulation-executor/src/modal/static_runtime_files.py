# pyright: reportAttributeAccessIssue=false
"""Shared Modal image layer for executor-owned static runtime files."""

from pathlib import Path

import modal

STATIC_RUNTIME_FILES_CONTAINER_DIR = (
    "/root/policyengine_simulation_executor/static_runtime_files"
)


def add_static_runtime_files(
    image: modal.Image,
    *,
    uv_project_dir: str,
) -> modal.Image:
    """Copy executor static files into a Modal image at the importable path."""

    local_directory = (
        str(
            Path(uv_project_dir)
            / "src"
            / "policyengine_simulation_executor"
            / "static_runtime_files"
        )
        if modal.is_local()
        else STATIC_RUNTIME_FILES_CONTAINER_DIR
    )
    return image.add_local_dir(
        local_directory,
        STATIC_RUNTIME_FILES_CONTAINER_DIR,
        copy=True,
    )
