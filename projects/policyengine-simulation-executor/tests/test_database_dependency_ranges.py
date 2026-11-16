"""The Modal images install the shared table-model library's database ranges.

The images install third-party packages only from the ``modal-simulation-image``
group, and the Stage 12 stores in them use ``policyengine-simulation-persistence``'s
models, so the group declares that library's SQLAlchemy and psycopg ranges.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SHARED_LIBRARY_PYPROJECT = (
    PROJECT_ROOT.parents[1]
    / "libs"
    / "policyengine-simulation-persistence"
    / "pyproject.toml"
)
DATABASE_PACKAGES = ("sqlalchemy", "psycopg")


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


def test_the_image_group_declares_the_shared_librarys_database_ranges() -> None:
    project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text("utf-8"))
    image_group = project["dependency-groups"]["modal-simulation-image"]
    shared = tomllib.loads(SHARED_LIBRARY_PYPROJECT.read_text("utf-8"))["project"][
        "dependencies"
    ]

    for package_name in DATABASE_PACKAGES:
        assert _requirement(image_group, package_name) == _requirement(
            shared, package_name
        )
