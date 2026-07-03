"""Shared test setup helpers for the simulation API project."""

import sys
from pathlib import Path


def ensure_project_root_on_path() -> None:
    """Add the project root to ``sys.path`` for local source imports."""
    project_root = Path(__file__).parent.parent
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
