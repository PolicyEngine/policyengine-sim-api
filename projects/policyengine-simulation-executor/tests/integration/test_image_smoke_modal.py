"""Integration wrapper for the executor image smoke (real Modal).

Deselected by default (see addopts); CI runs it via
.github/workflows/pr-image-smoke.yml on PRs touching image inputs.
"""

import os
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.integration
def test_executor_image_smoke():
    env_name = os.environ.get("MODAL_SMOKE_ENV", "staging")
    result = subprocess.run(
        [
            "uv",
            "run",
            "modal",
            "run",
            f"--env={env_name}",
            "src/modal/smoke_app.py",
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=2400,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "executor image smoke OK" in result.stdout


@pytest.mark.integration
def test_stage12_static_runtime_file_image_smoke():
    env_name = os.environ.get("MODAL_SMOKE_ENV", "staging")
    result = subprocess.run(
        [
            "uv",
            "run",
            "modal",
            "run",
            f"--env={env_name}",
            "src/modal/v2_static_runtime_files_smoke.py",
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=2400,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Stage 12 static runtime file smoke OK" in result.stdout
