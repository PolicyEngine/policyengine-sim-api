"""Print a package's locked version: ``dump_package_version.py <lock file> <package>``.

Exits 1, printing nothing, when the lock file or package is missing.
"""

import sys
from pathlib import Path

import tomllib


def get_package_version_from_lock(
    package_name: str, lock_file_path: str = "poetry.lock"
) -> str | None:
    """Return ``package_name``'s version from a TOML lock file, or None if absent.

    A missing lock file also returns None; uv and poetry locks share the
    ``[[package]]`` name/version layout read here.
    """
    try:
        with Path(lock_file_path).open("rb") as f:
            lock_data = tomllib.load(f)
    except FileNotFoundError:
        return None

    for package in lock_data.get("package", []):
        if package.get("name") == package_name:
            return package.get("version")

    return None


version = get_package_version_from_lock(sys.argv[2], sys.argv[1])

if version is None:
    sys.exit(1)

print(version)
sys.exit(0)
