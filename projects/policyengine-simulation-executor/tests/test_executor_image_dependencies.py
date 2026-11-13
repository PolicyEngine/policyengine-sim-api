"""The executor's Docker image can install every dependency it declares.

The image's build context is the repository root, and its Dockerfile copies
only some directories before ``uv sync --frozen --no-dev``. A main dependency
installed from a local path outside those directories can't be found, and
the image's build fails. A development group's local packages, such as the
gateway or the Simulation Entrypoint, are not installed there.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
REPOSITORY = PROJECT.parents[1]
DEPENDENCY_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*")


def _copied_directories() -> list[Path]:
    """Return each repository directory the Dockerfile copies into the image."""
    copied = []
    for dockerfile_line in (PROJECT / "Dockerfile").read_text().splitlines():
        words = dockerfile_line.split()
        if len(words) == 3 and words[0] == "COPY" and not words[1].startswith("--"):
            copied.append((REPOSITORY / words[1]).resolve())
    return copied


def test_every_main_dependency_from_a_local_path_is_in_the_images_build() -> None:
    pyproject = tomllib.loads((PROJECT / "pyproject.toml").read_text())
    sources = pyproject["tool"]["uv"]["sources"]
    main_dependencies = {
        DEPENDENCY_NAME.match(requirement).group(0)
        for requirement in pyproject["project"]["dependencies"]
    }
    local_main_dependencies = {
        name: (PROJECT / source["path"]).resolve()
        for name, source in sources.items()
        if "path" in source and name in main_dependencies
    }
    copied = _copied_directories()

    assert local_main_dependencies
    outside_the_build = sorted(
        name
        for name, path in local_main_dependencies.items()
        if not any(path.is_relative_to(directory) for directory in copied)
    )
    assert outside_the_build == []
