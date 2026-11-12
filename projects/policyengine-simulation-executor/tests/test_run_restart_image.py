"""The restart check imports with only the packages the sweep's image installs.

The sweep's Modal image installs the executor's locked
``modal-simulation-image`` group alone, and Modal adds its own client; the
restart check's code comes in as local sources. The scheduled function
imports that code only when it runs, so ``modal deploy`` would not notice a
package the image lacks. Here the import runs in a process that refuses
every other installed distribution's modules.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from importlib import metadata
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
IMAGE_GROUP = "modal-simulation-image"
# Modal installs its client into every image it builds.
MODAL_CLIENT = "modal"
# The local sources the sweep's image adds, which the lock doesn't install.
LOCAL_SOURCES = frozenset(
    {
        "policyengine-simulation-executor",
        "policyengine-simulation-observability",
        "policyengine-simulation-contract",
        "policyengine-stage12-persistence",
        "policyengine-simulation-persistence",
        "policyengine-simulation-entry",
    }
)
# What the scheduled function's body imports.
RESTART_MODULES = (
    "policyengine_simulation_entry.run_restart_check",
    "policyengine_simulation_observability.observability",
)


def _normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _image_distributions() -> set[str]:
    """Return the locked image group's distributions, with their dependencies."""
    lock = tomllib.loads((PROJECT / "uv.lock").read_text())
    packages = {_normalized(package["name"]): package for package in lock["package"]}
    project = packages["policyengine-simulation-executor"]
    wanted = [
        (_normalized(dependency["name"]), tuple(dependency.get("extra", ())))
        for dependency in project["dev-dependencies"][IMAGE_GROUP]
    ]
    wanted.append((MODAL_CLIENT, ()))
    installed: set[str] = set()
    seen: set[tuple[str, tuple[str, ...]]] = set()
    while wanted:
        name, extras = wanted.pop()
        if (name, extras) in seen or name not in packages:
            continue
        seen.add((name, extras))
        installed.add(name)
        package = packages[name]
        dependencies = list(package.get("dependencies", ()))
        for extra in extras:
            dependencies.extend(package.get("optional-dependencies", {}).get(extra, ()))
        wanted.extend(
            (_normalized(dependency["name"]), tuple(dependency.get("extra", ())))
            for dependency in dependencies
        )
    return installed


def _top_level_modules(distribution: metadata.Distribution) -> set[str]:
    """Return the importable top-level names a distribution installs."""
    listed = distribution.read_text("top_level.txt")
    if listed:
        return {name.strip() for name in listed.splitlines() if name.strip()}
    modules = set()
    for installed_file in distribution.files or ():
        first = installed_file.parts[0]
        if first.endswith((".dist-info", ".data")) or first in {"..", "__pycache__"}:
            continue
        if len(installed_file.parts) == 1 and installed_file.suffix != ".py":
            continue
        modules.add(first.removesuffix(".py"))
    return modules


def _refused_modules() -> set[str]:
    """Return the modules only distributions outside the image install."""
    allowed = _image_distributions() | LOCAL_SOURCES
    allowed_modules: set[str] = set()
    other_modules: set[str] = set()
    for distribution in metadata.distributions():
        modules = _top_level_modules(distribution)
        if _normalized(distribution.metadata["Name"]) in allowed:
            allowed_modules |= modules
        else:
            other_modules |= modules
    return other_modules - allowed_modules


def test_the_restart_check_imports_with_only_the_sweep_images_packages(
    tmp_path,
) -> None:
    refused = _refused_modules()
    # The executor's own environment holds packages the image doesn't.
    assert {"jwt", "uvicorn"} <= refused
    script = tmp_path / "import_restart_check.py"
    script.write_text(
        "import importlib, sys\n"
        f"refused = {sorted(refused)!r}\n"
        "class Refuse:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name.split('.')[0] in refused:\n"
        "            raise ImportError(f'not in the sweep image: {name}')\n"
        "sys.meta_path.insert(0, Refuse())\n"
        f"for module in {list(RESTART_MODULES)!r}:\n"
        "    importlib.import_module(module)\n",
        encoding="utf-8",
    )

    completed = subprocess.run(  # the test's own interpreter and script
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
