"""The library declares the shared table-model library's database ranges.

Its stores use ``policyengine-simulation-persistence``'s models, so they run on
the SQLAlchemy and psycopg releases that library declares, and declaring a
wider range would name versions the locks can't take.
"""

from __future__ import annotations

import re
from pathlib import Path

import tomllib

LIBRARY_ROOT = Path(__file__).resolve().parents[1]
SHARED_LIBRARY_ROOT = LIBRARY_ROOT.parent / "policyengine-simulation-persistence"
DATABASE_PACKAGES = ("sqlalchemy", "psycopg")


def _requirements(pyproject: Path) -> list[str]:
    return tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"][
        "dependencies"
    ]


def _requirement(requirements: list[str], package_name: str) -> str:
    """Return the one requirement naming ``package_name``, extras included."""
    matching = [
        requirement
        for requirement in requirements
        if (name := re.match(r"[A-Za-z0-9_.-]+", requirement)) is not None
        and name.group(0).lower() == package_name
    ]
    assert len(matching) == 1, (package_name, matching)
    return matching[0]


def test_the_library_declares_the_shared_librarys_database_ranges() -> None:
    own = _requirements(LIBRARY_ROOT / "pyproject.toml")
    shared = _requirements(SHARED_LIBRARY_ROOT / "pyproject.toml")

    for package_name in DATABASE_PACKAGES:
        assert _requirement(own, package_name) == _requirement(shared, package_name)
