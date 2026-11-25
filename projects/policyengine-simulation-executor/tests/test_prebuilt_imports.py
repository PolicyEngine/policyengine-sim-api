"""The level handlers import without the v2 database packages.

The run_simulation app's images (the executor, the precompute and the image
smoke) install neither ``policyengine_stage12_persistence`` nor
``policyengine_simulation_persistence``, but they load the handlers. Each
check runs in a fresh interpreter with both packages blocked, so a module
cached by an earlier test cannot hide an import that needs them.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SOURCE_FOLDER = Path(__file__).resolve().parents[1] / "src"
BLOCKED_PACKAGES = (
    "policyengine_stage12_persistence",
    "policyengine_simulation_persistence",
)
_BLOCKING_PRELUDE = """
import importlib
import importlib.abc
import sys

blocked = {blocked!r}


class BlockDatabasePackages(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in blocked:
            raise ModuleNotFoundError(f"No module named {{name!r}}")
        return None


sys.meta_path.insert(0, BlockDatabasePackages())
"""


def _run_without_database_packages(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-c",
            _BLOCKING_PRELUDE.format(blocked=BLOCKED_PACKAGES) + code,
        ],
        cwd=SOURCE_FOLDER,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    "module",
    [
        "policyengine_simulation_executor.stage12_cache.levels_us",
        "policyengine_simulation_executor.stage12_cache.levels_uk",
        "policyengine_simulation_executor.national_partition",
    ],
)
def test_imports_without_the_database_packages(module):
    result = _run_without_database_packages(f"importlib.import_module({module!r})\n")

    assert result.returncode == 0, result.stderr


def test_the_us_settings_load_without_the_database_packages():
    result = _run_without_database_packages(
        "from policyengine_simulation_executor.stage12_cache import levels\n"
        "assert len(levels.prebuilt_for('us').handlers) == 4\n"
    )

    assert result.returncode == 0, result.stderr
