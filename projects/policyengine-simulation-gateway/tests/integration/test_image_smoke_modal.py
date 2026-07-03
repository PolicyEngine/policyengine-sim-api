"""Integration wrapper for the gateway image smoke (real Modal).

Deselected by default (see addopts); CI runs it via
.github/workflows/pr-image-smoke.yml on PRs touching image inputs.
"""

import os
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.integration
def test_gateway_image_smoke():
    env_name = os.environ.get("MODAL_SMOKE_ENV", "staging")
    result = subprocess.run(
        [
            "uv",
            "run",
            "modal",
            "run",
            f"--env={env_name}",
            "src/policyengine_simulation_gateway/smoke_app.py",
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=1200,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "gateway image smoke OK" in result.stdout
