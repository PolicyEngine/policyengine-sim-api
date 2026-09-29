"""Structured operations used by update-policyengine-package.sh."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import tomllib

COMPONENT_PACKAGES = frozenset(
    {
        "policyengine-core",
        "policyengine-us",
        "policyengine-uk",
        "spm-calculator",
    }
)


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _json_from_stdin() -> Any:
    return json.load(sys.stdin)


def _requirement_name(requirement: str) -> str:
    return re.split(r"[\s\[<>=!~;@]", requirement, maxsplit=1)[0]


def _project_dependencies(pyproject: Path) -> list:
    parsed = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    dependencies = _mapping(parsed.get("project")).get("dependencies", [])
    return dependencies if isinstance(dependencies, list) else []


def current_version(pyproject: Path, package: str) -> str:
    """Return the exact PolicyEngine models-extra version used by the project."""
    prefix = f"{package}[models]=="
    matches = [
        dependency.removeprefix(prefix)
        for dependency in _project_dependencies(pyproject)
        if isinstance(dependency, str) and dependency.startswith(prefix)
    ]
    if len(matches) != 1:
        raise SystemExit(
            f"Expected one {package}[models] requirement in project.dependencies; "
            f"found {matches!r}"
        )
    return matches[0]


def update_requirements(
    pyproject: Path,
    package: str,
    current: str,
    latest: str,
) -> None:
    """Update the project and Modal wrapper requirements atomically."""
    text = pyproject.read_text(encoding="utf-8")
    parsed = tomllib.loads(text)
    old_requirement = f"{package}[models]=={current}"
    new_requirement = f"{package}[models]=={latest}"
    dependency_groups = _mapping(parsed.get("dependency-groups"))
    requirements = {
        "project.dependencies": _mapping(parsed.get("project")).get("dependencies", []),
        "dependency-groups.modal-simulation-image": dependency_groups.get(
            "modal-simulation-image", []
        ),
    }

    for location, dependencies in requirements.items():
        dependencies = dependencies if isinstance(dependencies, list) else []
        matches = [
            dependency
            for dependency in dependencies
            if isinstance(dependency, str)
            and dependency.startswith(f"{package}[models]==")
        ]
        if matches != [old_requirement]:
            raise SystemExit(
                f"Expected {old_requirement} in {location}; found {matches!r}"
            )

        redundant_wrapper_requirements = [
            dependency
            for dependency in dependencies
            if isinstance(dependency, str)
            and dependency != old_requirement
            and _requirement_name(dependency) == package
        ]
        if redundant_wrapper_requirements:
            raise SystemExit(
                f"Expected only {old_requirement} for {package} in {location}; "
                f"found {redundant_wrapper_requirements!r}"
            )

        direct_component_requirements = [
            dependency
            for dependency in dependencies
            if isinstance(dependency, str)
            and _requirement_name(dependency) in COMPONENT_PACKAGES
        ]
        if direct_component_requirements:
            raise SystemExit(
                f"Expected {location} to obtain component packages from "
                f"{package}[models]; found {direct_component_requirements!r}"
            )

    old_pin = f'"{old_requirement}"'
    new_pin = f'"{new_requirement}"'
    if text.count(old_pin) != len(requirements):
        raise SystemExit(
            f"Expected {old_pin} {len(requirements)} times in {pyproject}; "
            f"found {text.count(old_pin)}"
        )

    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=pyproject.parent,
        prefix=f".{pyproject.name}.",
        delete=False,
    ) as temporary:
        temporary.write(text.replace(old_pin, new_pin))
        temporary_path = temporary.name
    os.replace(temporary_path, pyproject)


def find_issue(title: str) -> None:
    """Print the lowest matching issue number from paginated GitHub output."""
    pages = _json_from_stdin()
    if not isinstance(pages, list):
        raise SystemExit("GitHub issue response must be a list of pages")
    matches = sorted(
        item["number"]
        for page in pages
        if isinstance(page, list)
        for item in page
        if isinstance(item, Mapping)
        and "pull_request" not in item
        and item.get("title") == title
        and isinstance(item.get("number"), int)
    )
    if matches:
        print(matches[0])


def verify_issue(expected_number: int, expected_title: str) -> None:
    """Validate the issue selected for an automated update."""
    issue = _mapping(_json_from_stdin())
    if issue.get("number") != expected_number:
        raise SystemExit("Resolved update issue has an unexpected number")
    if issue.get("state") != "OPEN":
        raise SystemExit("Resolved update issue is not open")
    if issue.get("title") != expected_title:
        raise SystemExit("Resolved update issue has an unexpected title")


def verify_pr(expected_repository: str) -> None:
    """Validate that an automated pull request is a canonical draft."""
    pull_request = _mapping(_json_from_stdin())
    if pull_request.get("isDraft") is not True:
        raise SystemExit("Automated policyengine update PR is not a draft")
    head_repository = _mapping(pull_request.get("headRepository"))
    if head_repository.get("nameWithOwner") != expected_repository:
        raise SystemExit(
            "Automated policyengine update PR is not from the canonical repository"
        )


def latest_version(package: str) -> None:
    """Print a package version from the PyPI JSON response."""
    payload = _mapping(_json_from_stdin())
    version = _mapping(payload.get("info")).get("version")
    if not isinstance(version, str) or not version:
        raise SystemExit(f"PyPI response has no latest version for {package}")
    print(version)


def bundle_versions() -> None:
    """Print the package and data versions selected by PolicyEngine.py."""
    from policyengine.bundle import get_current_bundle

    bundle = _mapping(get_current_bundle())
    packages = _mapping(bundle.get("packages"))
    data_releases = _mapping(bundle.get("data_releases"))

    def package_version(name: str) -> str:
        version = _mapping(packages.get(name)).get("version")
        if not isinstance(version, str) or not version:
            raise SystemExit(f"Bundle has no version for {name}")
        return version

    def data_release_version(country: str) -> str:
        release = _mapping(data_releases.get(country))
        version = release.get("version") or _mapping(release.get("data_package")).get(
            "version"
        )
        if not isinstance(version, str) or not version:
            raise SystemExit(f"Bundle has no data release version for {country}")
        return version

    outputs = {
        "policyengine_version": package_version("policyengine"),
        "policyengine_core_version": package_version("policyengine-core"),
        "spm_calculator_version": package_version("spm-calculator"),
        "us_version": package_version("policyengine-us"),
        "us_data_version": data_release_version("us"),
        "uk_version": package_version("policyengine-uk"),
        "uk_data_version": data_release_version("uk"),
    }
    for name, version in outputs.items():
        print(f"{name}={version}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    current = commands.add_parser("current-version")
    current.add_argument("--pyproject", type=Path, required=True)
    current.add_argument("--package", required=True)

    update = commands.add_parser("update-requirements")
    update.add_argument("--pyproject", type=Path, required=True)
    update.add_argument("--package", required=True)
    update.add_argument("--current", required=True)
    update.add_argument("--latest", required=True)

    issue_search = commands.add_parser("find-issue")
    issue_search.add_argument("--title", required=True)

    issue_check = commands.add_parser("verify-issue")
    issue_check.add_argument("--number", type=int, required=True)
    issue_check.add_argument("--title", required=True)

    pr_check = commands.add_parser("verify-pr")
    pr_check.add_argument("--repository", required=True)

    pypi = commands.add_parser("latest-version")
    pypi.add_argument("--package", required=True)

    commands.add_parser("bundle-versions")
    return parser


def main() -> None:
    args = _parser().parse_args()
    if args.command == "current-version":
        print(current_version(args.pyproject, args.package))
    elif args.command == "update-requirements":
        update_requirements(args.pyproject, args.package, args.current, args.latest)
    elif args.command == "find-issue":
        find_issue(args.title)
    elif args.command == "verify-issue":
        verify_issue(args.number, args.title)
    elif args.command == "verify-pr":
        verify_pr(args.repository)
    elif args.command == "latest-version":
        latest_version(args.package)
    elif args.command == "bundle-versions":
        bundle_versions()


if __name__ == "__main__":
    main()
