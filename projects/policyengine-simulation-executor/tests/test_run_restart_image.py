"""The restart check imports with only the packages the sweep's image installs.

The sweep's Modal image installs the executor's locked
``modal-simulation-image`` group alone, and Modal adds its own client; the
restart check's code comes in as local sources. The scheduled function
imports that code only when it runs, so ``modal deploy`` would not notice a
package the image lacks. Here the import runs in a process that refuses the
top-level modules only other installed distributions provide, then every
module it loaded must come from the standard library, a local source, or a
distribution the image installs: a module under a namespace that an image
distribution shares, or from an editable install, is caught there.
"""

from __future__ import annotations

import json
import re
import site
import subprocess
import sys
import sysconfig
import tomllib
from importlib import metadata
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
REPOSITORY = PROJECT.parents[1]
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
# Where each local source's package lies in the repository.
LOCAL_SOURCE_DIRECTORIES = (
    PROJECT / "src" / "policyengine_simulation_executor",
    REPOSITORY
    / "projects/policyengine-simulation-entry/src/policyengine_simulation_entry",
    *(
        REPOSITORY / "libs" / library / "src" / library.replace("-", "_")
        for library in (
            "policyengine-simulation-observability",
            "policyengine-simulation-contract",
            "policyengine-stage12-persistence",
            "policyengine-simulation-persistence",
        )
    ),
)
# The script's own module, which multiprocessing, once imported, also names
# ``__mp_main__``; it is not the restart check's.
NOT_THE_RESTART_CHECK = ("__mp_main__",)
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


def _image_files() -> set[Path]:
    """Return every file the image's distributions install here."""
    allowed = _image_distributions()
    return {
        Path(str(distribution.locate_file(installed_file))).resolve()
        for distribution in metadata.distributions()
        if _normalized(distribution.metadata["Name"]) in allowed
        for installed_file in distribution.files or ()
    }


def _from_the_image(module_file: Path, image_files: set[Path]) -> bool:
    """Return whether a loaded module's file is one the sweep's image has."""
    site_packages = [Path(path).resolve() for path in site.getsitepackages()]
    standard_library = [
        Path(sysconfig.get_paths()[name]).resolve() for name in ("stdlib", "platstdlib")
    ]
    if module_file in image_files:
        return True
    if any(module_file.is_relative_to(source) for source in LOCAL_SOURCE_DIRECTORIES):
        return True
    return any(
        module_file.is_relative_to(library) for library in standard_library
    ) and not any(module_file.is_relative_to(packages) for packages in site_packages)


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
    loaded_list = tmp_path / "loaded_module_files.json"
    script.write_text(
        "import importlib, sys\n"
        # Only what the import adds is checked, not what started the process.
        "loaded_before = set(sys.modules)\n"
        f"refused = {sorted(refused)!r}\n"
        "class Refuse:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name.split('.')[0] in refused:\n"
        "            raise ImportError(f'not in the sweep image: {name}')\n"
        "sys.meta_path.insert(0, Refuse())\n"
        f"for module in {list(RESTART_MODULES)!r}:\n"
        "    importlib.import_module(module)\n"
        "import json\n"
        "loaded = sorted({getattr(module, '__file__', None) or ''\n"
        "                 for name, module in list(sys.modules.items())\n"
        "                 if name not in loaded_before\n"
        f"                 and name not in {list(NOT_THE_RESTART_CHECK)!r}}}\n"
        "         - {''})\n"
        f"open({str(loaded_list)!r}, 'w').write(json.dumps(loaded))\n",
        encoding="utf-8",
    )

    completed = subprocess.run(  # the test's own interpreter and script
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    image_files = _image_files()
    loaded = [Path(name).resolve() for name in json.loads(loaded_list.read_text())]
    assert any(
        module_file.is_relative_to(LOCAL_SOURCE_DIRECTORIES[1])
        for module_file in loaded
    )
    outside_the_image = [
        str(module_file)
        for module_file in loaded
        if not _from_the_image(module_file, image_files)
    ]
    assert outside_the_image == []
