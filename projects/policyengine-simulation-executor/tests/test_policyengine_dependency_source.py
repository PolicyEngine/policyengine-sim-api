"""Regression tests for the policyengine dependency version configuration."""

import os
import subprocess
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"
MODAL_APP_PATH = REPO_ROOT / "src" / "modal" / "app.py"
POLICYENGINE_DEPENDENCY_PREFIX = "policyengine=="
POLICYENGINE_CORE_DEPENDENCY_PREFIX = "policyengine-core=="
COUNTRY_PACKAGES = {
    "us": "policyengine-us",
    "uk": "policyengine-uk",
}
MODAL_APP_MODULE = "src.modal.app"
VERSION_ENV = {
    "POLICYENGINE_VERSION": "4.18.3",
    "POLICYENGINE_CORE_VERSION": "3.27.1",
    "POLICYENGINE_US_VERSION": "1.729.0",
    "POLICYENGINE_UK_VERSION": "2.89.2",
}


def _load_toml(path: Path) -> dict:
    with path.open("rb") as file:
        return tomllib.load(file)


def _get_pyproject_policyengine_dependency(pyproject: dict) -> str:
    dependencies = pyproject["project"]["dependencies"]
    return next(
        dep for dep in dependencies if dep.startswith(POLICYENGINE_DEPENDENCY_PREFIX)
    )


def _get_pyproject_policyengine_core_dependency(pyproject: dict) -> str:
    dependencies = pyproject["project"]["dependencies"]
    return next(
        dep
        for dep in dependencies
        if dep.startswith(POLICYENGINE_CORE_DEPENDENCY_PREFIX)
    )


def _get_dependency_pin(pyproject: dict, package: str) -> str:
    dependencies = pyproject["project"]["dependencies"]
    prefix = f"{package}=="
    return next(
        dep.removeprefix(prefix) for dep in dependencies if dep.startswith(prefix)
    )


def test_policyengine_dependency_version_is_pinned_consistently():
    from src.modal.dependency_pins import project_dependency_pin

    pyproject = _load_toml(PYPROJECT_PATH)
    pyproject_dependency = _get_pyproject_policyengine_dependency(pyproject)
    pyproject_core_dependency = _get_pyproject_policyengine_core_dependency(pyproject)

    assert pyproject_dependency.startswith(POLICYENGINE_DEPENDENCY_PREFIX)
    assert pyproject_core_dependency.startswith(POLICYENGINE_CORE_DEPENDENCY_PREFIX)
    assert (
        f"policyengine=={project_dependency_pin('policyengine')}"
        == pyproject_dependency
    )
    assert (
        f"policyengine-core=={project_dependency_pin('policyengine-core')}"
        == pyproject_core_dependency
    )


def test_modal_app_reads_policyengine_pins_from_pyproject():
    modal_source = MODAL_APP_PATH.read_text(encoding="utf-8")

    assert '"policyengine==4.10.0"' not in modal_source
    assert '"policyengine-core==3.26.1"' not in modal_source
    assert "project_dependency_pin" in modal_source
    assert '"policyengine"' in modal_source
    assert '"policyengine-core"' in modal_source
    assert "POLICYENGINE_CORE_VERSION" in modal_source
    assert ".env(VERSION_ENV)" in modal_source


def test_modal_app_name_is_keyed_to_policyengine_py_version():
    modal_source = MODAL_APP_PATH.read_text(encoding="utf-8")

    assert "def get_app_name(policyengine_version: str)" in modal_source
    assert "policyengine-simulation-py" in modal_source
    assert "policyengine-simulation-us" not in modal_source


def test_country_package_pins_match_policyengine_bundle():
    from policyengine_simulation_executor.release_bundle import get_country_release_bundle

    pyproject = _load_toml(PYPROJECT_PATH)

    for country, package in COUNTRY_PACKAGES.items():
        assert (
            _get_dependency_pin(pyproject, package)
            == get_country_release_bundle(country).model_version
        )


def _modal_import_env() -> dict[str, str]:
    env = os.environ.copy()
    for env_var in VERSION_ENV:
        env.pop(env_var, None)
    return env


def test_modal_app_remote_import_uses_image_version_env():
    code = """
import json
import modal

modal.is_local = lambda: False

from policyengine_simulation_executor import release_bundle
from src.modal import dependency_pins

def fail(message):
    raise AssertionError(message)

dependency_pins.project_dependency_pin = lambda package: fail(
    f"read pyproject for {package}"
)
release_bundle.get_bundled_country_model_version = lambda country: fail(
    f"read bundle manifest for {country}"
)

import src.modal.app as app

print(json.dumps({
    "version_env": app.VERSION_ENV,
    "app_name": app.APP_NAME,
}))
"""
    env = _modal_import_env()
    env.update(VERSION_ENV)
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    output = result.stdout.strip()
    assert '"version_env":' in output
    assert '"POLICYENGINE_CORE_VERSION": "3.27.1"' in output
    assert '"app_name": "policyengine-simulation-py4-18-3"' in output


def test_modal_app_remote_import_fails_clearly_without_version_env():
    code = """
import modal

modal.is_local = lambda: False

import src.modal.app
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        env=_modal_import_env(),
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "POLICYENGINE_VERSION must be set" in result.stderr
