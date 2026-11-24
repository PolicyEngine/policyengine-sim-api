"""The executor's Modal client reloads Secret values on a memory-snapshot restore.

Modal 1.6.4 is the first release whose containers reload Secret values when
they restore from a memory snapshot; on earlier releases a restored container
keeps the values from when the snapshot was taken. A deployed app's containers
run the Modal client that deployed it, and every app deployed or run from this
project uses the client in ``uv.lock``: the deploy workflow runs
``uv run --frozen modal deploy`` here, and the precompute and smoke scripts run
``uv run --frozen modal run`` here.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIRST_MODAL_VERSION_RELOADING_SECRETS_ON_RESTORE = Version("1.6.4")
LOWER_BOUND_OPERATORS = (">=", "~=", "==")


def _modal_requirement() -> Requirement:
    project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text("utf-8"))
    modal_requirements = [
        requirement
        for requirement in map(Requirement, project["project"]["dependencies"])
        if canonicalize_name(requirement.name) == "modal"
    ]
    assert len(modal_requirements) == 1, modal_requirements
    return modal_requirements[0]


def _locked_modal_versions() -> list[Version]:
    lock = tomllib.loads((PROJECT_ROOT / "uv.lock").read_text("utf-8"))
    return [
        Version(package["version"])
        for package in lock["package"]
        if canonicalize_name(package["name"]) == "modal"
    ]


def test_the_project_requires_a_modal_client_that_reloads_secrets_on_restore() -> None:
    lower_bounds = [
        Version(specifier.version)
        for specifier in _modal_requirement().specifier
        if specifier.operator in LOWER_BOUND_OPERATORS
    ]

    assert lower_bounds, "the Modal requirement has no lower bound"
    assert max(lower_bounds) >= FIRST_MODAL_VERSION_RELOADING_SECRETS_ON_RESTORE


def test_the_lock_installs_a_modal_client_that_reloads_secrets_on_restore() -> None:
    locked_versions = _locked_modal_versions()

    assert locked_versions, "uv.lock has no modal package"
    assert all(
        locked_version >= FIRST_MODAL_VERSION_RELOADING_SECRETS_ON_RESTORE
        for locked_version in locked_versions
    ), locked_versions
