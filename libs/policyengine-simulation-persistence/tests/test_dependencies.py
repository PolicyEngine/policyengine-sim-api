"""The library runs on the SQLAlchemy the Simulation Entrypoint's image ships.

The entrypoint installs this library from its own lock, so the library's tests
are only evidence for the service when they run on the same minor version.
"""

from __future__ import annotations

from pathlib import Path

import tomllib

LIBRARY_ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT_LOCK = (
    LIBRARY_ROOT.parents[1] / "projects" / "policyengine-simulation-entry" / "uv.lock"
)


def _locked_version(lock_path: Path, package_name: str) -> str:
    lock = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    [version] = [
        package["version"]
        for package in lock["package"]
        if package["name"] == package_name
    ]
    return version


def _minor_version(version: str) -> tuple[int, int]:
    major, minor = version.split(".")[:2]
    return int(major), int(minor)


def test_the_library_caps_sqlalchemy_below_the_next_minor_version() -> None:
    project = tomllib.loads(
        (LIBRARY_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]

    assert "sqlalchemy>=2,<2.1" in project["dependencies"]


def test_the_library_is_tested_on_the_sqlalchemy_the_entrypoint_ships() -> None:
    library_version = _locked_version(LIBRARY_ROOT / "uv.lock", "sqlalchemy")
    entrypoint_version = _locked_version(ENTRYPOINT_LOCK, "sqlalchemy")

    assert _minor_version(library_version) == _minor_version(entrypoint_version)
