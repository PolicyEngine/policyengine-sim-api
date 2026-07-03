"""Fixtures and shared constants for Modal script tests."""

import os
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent.parent
SCRIPTS_DIR = REPO_ROOT / ".github" / "scripts"


@pytest.fixture
def temp_github_output():
    """Create a temporary file to simulate ``GITHUB_OUTPUT``."""
    with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".txt") as file:
        yield file.name
    os.unlink(file.name)


@pytest.fixture
def temp_github_step_summary():
    """Create a temporary file to simulate ``GITHUB_STEP_SUMMARY``."""
    with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".md") as file:
        yield file.name
    os.unlink(file.name)


@pytest.fixture
def all_modal_scripts():
    """Return all ``modal-*.sh`` scripts in the repository."""
    return list(SCRIPTS_DIR.glob("modal-*.sh"))
