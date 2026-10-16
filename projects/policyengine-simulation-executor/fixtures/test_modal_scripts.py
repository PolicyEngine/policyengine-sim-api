"""Fixtures and shared constants for Modal script tests."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

REPO_ROOT = Path(__file__).parent.parent.parent.parent
SCRIPTS_DIR = REPO_ROOT / ".github" / "scripts"


@pytest.fixture
def temp_github_output() -> Iterator[str]:
    """Create a temporary file to simulate ``GITHUB_OUTPUT``."""
    with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".txt") as file:
        yield file.name
    os.unlink(file.name)


@pytest.fixture
def temp_github_step_summary() -> Iterator[str]:
    """Create a temporary file to simulate ``GITHUB_STEP_SUMMARY``."""
    with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".md") as file:
        yield file.name
    os.unlink(file.name)


@pytest.fixture
def all_modal_scripts() -> list[Path]:
    """Return Modal and full-stack simulation deployment scripts."""
    return [
        *SCRIPTS_DIR.glob("modal-*.sh"),
        *SCRIPTS_DIR.glob("simulation-*.sh"),
    ]
