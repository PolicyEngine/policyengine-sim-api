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


def _json_from_stdin() -> object:
    return json.load(sys.stdin)


def _write_result(result: object) -> None:
    """Print one result line for update-policyengine-package.sh to capture."""
    print(result)


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
        msg = (
            f"Expected one {package}[models] requirement in project.dependencies; "
            f"found {matches!r}"
        )
        raise SystemExit(msg)
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
            msg = f"Expected {old_requirement} in {location}; found {matches!r}"
            raise SystemExit(msg)

        redundant_wrapper_requirements = [
            dependency
            for dependency in dependencies
            if isinstance(dependency, str)
            and dependency != old_requirement
            and _requirement_name(dependency) == package
        ]
        if redundant_wrapper_requirements:
            msg = (
                f"Expected only {old_requirement} for {package} in {location}; "
                f"found {redundant_wrapper_requirements!r}"
            )
            raise SystemExit(msg)

        direct_component_requirements = [
            dependency
            for dependency in dependencies
            if isinstance(dependency, str)
            and _requirement_name(dependency) in COMPONENT_PACKAGES
        ]
        if direct_component_requirements:
            msg = (
                f"Expected {location} to obtain component packages from "
                f"{package}[models]; found {direct_component_requirements!r}"
            )
            raise SystemExit(msg)

    old_pin = f'"{old_requirement}"'
    new_pin = f'"{new_requirement}"'
    if text.count(old_pin) != len(requirements):
        msg = (
            f"Expected {old_pin} {len(requirements)} times in {pyproject}; "
            f"found {text.count(old_pin)}"
        )
        raise SystemExit(msg)

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
        msg = "GitHub issue response must be a list of pages"
        raise SystemExit(msg)
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
        _write_result(matches[0])


def verify_issue(expected_number: int, expected_title: str) -> None:
    """Validate the issue selected for an automated update."""
    issue = _mapping(_json_from_stdin())
    if issue.get("number") != expected_number:
        msg = "Resolved update issue has an unexpected number"
        raise SystemExit(msg)
    if issue.get("state") != "OPEN":
        msg = "Resolved update issue is not open"
        raise SystemExit(msg)
    if issue.get("title") != expected_title:
        msg = "Resolved update issue has an unexpected title"
        raise SystemExit(msg)


def verify_pr(expected_repository: str) -> None:
    """Validate that an automated pull request is a canonical draft."""
    pull_request = _mapping(_json_from_stdin())
    if pull_request.get("isDraft") is not True:
        msg = "Automated policyengine update PR is not a draft"
        raise SystemExit(msg)
    head_repository = _mapping(pull_request.get("headRepository"))
    if head_repository.get("nameWithOwner") != expected_repository:
        msg = "Automated policyengine update PR is not from the canonical repository"
        raise SystemExit(msg)


def latest_version(package: str) -> None:
    """Print a package version from the PyPI JSON response."""
    payload = _mapping(_json_from_stdin())
    version = _mapping(payload.get("info")).get("version")
    if not isinstance(version, str) or not version:
        msg = f"PyPI response has no latest version for {package}"
        raise SystemExit(msg)
    _write_result(version)


def bundle_versions() -> None:
    """Print the package and data versions selected by PolicyEngine.py."""
    from policyengine.bundle import (
        get_current_bundle,
    )

    bundle = _mapping(get_current_bundle())
    packages = _mapping(bundle.get("packages"))
    data_releases = _mapping(bundle.get("data_releases"))

    def package_version(name: str) -> str:
        version = _mapping(packages.get(name)).get("version")
        if not isinstance(version, str) or not version:
            msg = f"Bundle has no version for {name}"
            raise SystemExit(msg)
        return version

    def data_release_version(country: str) -> str:
        release = _mapping(data_releases.get(country))
        version = release.get("version") or _mapping(release.get("data_package")).get(
            "version"
        )
        if not isinstance(version, str) or not version:
            msg = f"Bundle has no data release version for {country}"
            raise SystemExit(msg)
        return version

    def data_package_version(country: str) -> str:
        release = _mapping(data_releases.get(country))
        version = _mapping(release.get("data_package")).get("version")
        return version if isinstance(version, str) else ""

    def dataset_sha256(country: str) -> str:
        release = _mapping(data_releases.get(country))
        default = release.get("default_dataset")
        certified = _mapping(release.get("certified_data_artifact"))
        if certified.get("dataset") == default and certified.get("sha256"):
            value = certified["sha256"]
        else:
            reference = _mapping(_mapping(release.get("datasets")).get(default))
            value = reference.get("sha256") or ""
        digest = str(value).strip().lower()
        if digest and not re.fullmatch(r"[0-9a-f]{64}", digest):
            msg = f"Bundle records a malformed {country} dataset SHA-256"
            raise SystemExit(msg)
        return digest

    def certificate_sha256(country: str) -> str:
        # Receipt-v1 certificate digest; older wrappers lack the helper.
        try:
            from policyengine.provenance.receipt import (
                certificate_sha256 as digest,
            )
        except ImportError:
            return ""
        release = data_releases.get(country)
        return digest(release) if isinstance(release, Mapping) else ""

    outputs = {
        "policyengine_version": package_version("policyengine"),
        "policyengine_core_version": package_version("policyengine-core"),
        "spm_calculator_version": package_version("spm-calculator"),
        "us_version": package_version("policyengine-us"),
        "us_data_version": data_release_version("us"),
        "uk_version": package_version("policyengine-uk"),
        "uk_data_version": data_release_version("uk"),
        "uk_data_package_version": data_package_version("uk"),
        "us_dataset_sha256": dataset_sha256("us"),
        "uk_dataset_sha256": dataset_sha256("uk"),
        "uk_certificate_sha256": certificate_sha256("uk"),
    }
    for name, version in outputs.items():
        _write_result(f"{name}={version}")


#: Release-dispatch fields compared with the installed bundle: (payload key
#: under client_payload.bundle.uk, bundle-versions output key). policyengine.py
#: sends data_release_version as the data package version ("1.58.0"), not the
#: release record's build-style version.
DISPATCH_CHECKS = (
    ("model_version", "uk_version"),
    ("core_version", "policyengine_core_version"),
    ("data_release_version", "uk_data_package_version"),
    ("dataset_sha256", "uk_dataset_sha256"),
    ("certificate_sha256", "uk_certificate_sha256"),
)
#: Checks an installed wrapper may be too old to report; skipped when empty.
OPTIONAL_INSTALLED_CHECKS = frozenset({"certificate_sha256"})
#: Qualification verdicts a promoted pair can carry.
PROMOTABLE_VERDICTS = frozenset({"qualified", "needs_review"})


def verify_dispatch() -> None:
    """Check a policyengine-release payload against the installed bundle.

    Reads the dispatch ``client_payload`` JSON from ``RELEASE_PAYLOAD`` and
    ``bundle-versions`` output from stdin. A payload without ``bundle.uk``
    (older releases, manual runs) is accepted. A field the payload names
    must equal the installed bundle's, and an installed bundle that cannot
    report it is a mismatch, except for the certificate digest, which older
    wrappers cannot compute. A qualification that says the pair was not
    promotable is refused. ``dispatch_verified`` is true only when at least
    one field was compared. Prints digests for the PR body.
    """
    raw = os.environ.get("RELEASE_PAYLOAD", "").strip()
    payload = json.loads(raw) if raw and raw != "null" else {}
    installed = {}
    for line in sys.stdin.read().splitlines():
        key, _, value = line.partition("=")
        if key:
            installed[key.strip()] = value.strip()
    if payload.get("version") and payload["version"] != installed.get(
        "policyengine_version"
    ):
        msg = (
            f"Dispatch is for policyengine {payload['version']}, but "
            f"{installed.get('policyengine_version')} is installed."
        )
        raise SystemExit(msg)
    expected = _mapping(_mapping(payload.get("bundle")).get("uk"))
    mismatches = []
    compared = 0
    for payload_key, installed_key in DISPATCH_CHECKS:
        claimed = expected.get(payload_key)
        actual = installed.get(installed_key, "")
        if claimed in (None, ""):
            continue
        if actual == "":
            if payload_key in OPTIONAL_INSTALLED_CHECKS:
                continue
            mismatches.append(
                f"{payload_key}: dispatch {claimed}, installed bundle reports none"
            )
            continue
        compared += 1
        claimed_text = str(claimed).strip()
        if payload_key.endswith("sha256"):
            claimed_text = claimed_text.lower()
        if claimed_text != actual:
            mismatches.append(f"{payload_key}: dispatch {claimed}, installed {actual}")
    if mismatches:
        raise SystemExit(
            "The release dispatch names a different UK bundle from the installed "
            "wheel: " + "; ".join(mismatches)
        )
    qualification = _mapping(_mapping(payload.get("qualification")).get("uk"))
    verdict = qualification.get("verdict") or ""
    receipt_sha256 = qualification.get("receipt_sha256") or ""
    if not isinstance(verdict, str) or not re.fullmatch(r"[a-z_]*", verdict):
        msg = "The release dispatch carries a malformed verdict"
        raise SystemExit(msg)
    if not isinstance(receipt_sha256, str) or not re.fullmatch(
        r"(?:[0-9a-f]{64})?", receipt_sha256
    ):
        msg = "The release dispatch carries a malformed receipt digest"
        raise SystemExit(msg)
    if qualification.get("promotable") is False or (
        verdict and verdict not in PROMOTABLE_VERDICTS
    ):
        msg = f"The release dispatch says the UK pair was not promotable ({verdict})"
        raise SystemExit(msg)
    _write_result(f"dispatch_verified={'true' if compared else 'false'}")
    _write_result(f"uk_qualification_verdict={verdict}")
    _write_result(f"uk_qualification_receipt_sha256={receipt_sha256}")


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
    commands.add_parser("verify-dispatch")
    return parser


def main() -> None:
    """Run one subcommand for update-policyengine-package.sh.

    Results go to stdout for the shell script to capture; a failed check exits
    non-zero through ``SystemExit`` with the reason as its message.
    """
    args = _parser().parse_args()
    if args.command == "current-version":
        _write_result(current_version(args.pyproject, args.package))
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
    elif args.command == "verify-dispatch":
        verify_dispatch()


if __name__ == "__main__":
    main()
