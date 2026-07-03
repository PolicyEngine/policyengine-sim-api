"""Helpers for reading pinned project dependencies."""

from __future__ import annotations

import tomllib
from functools import lru_cache
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[2]
PYPROJECT_PATH = PROJECT_DIR / "pyproject.toml"


@lru_cache
def _project_dependencies() -> tuple[str, ...]:
    pyproject = tomllib.loads(PYPROJECT_PATH.read_text(encoding="utf-8"))
    return tuple(pyproject["project"]["dependencies"])


def project_dependency_pin(package: str) -> str:
    prefix = f"{package}=="
    for dependency in _project_dependencies():
        if dependency.startswith(prefix):
            return dependency.removeprefix(prefix)
    raise ValueError(f"Dependency {package!r} is not pinned in {PYPROJECT_PATH}")
