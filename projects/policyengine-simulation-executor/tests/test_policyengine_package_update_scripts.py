"""Unit tests for policyengine package updater scripts."""

from __future__ import annotations

import subprocess
from pathlib import Path

from fixtures.test_policyengine_package_update_scripts import (
    SCRIPT,
    install_fake_gh,
    install_fake_git,
    install_fake_uv,
    run_updater,
    updater_env,
)

pytest_plugins = ("fixtures.test_policyengine_package_update_scripts",)


def test_update_policyengine_package_script_has_valid_bash_syntax() -> None:
    result = subprocess.run(
        ["bash", "-n", str(SCRIPT)],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_update_policyengine_package_rejects_unknown_argument(
    fake_bin: Path, fake_repo: Path, tmp_path: Path
) -> None:
    git_log = tmp_path / "git.log"
    install_fake_git(fake_bin, root=fake_repo, log=git_log)

    result = run_updater(
        "policyengine-us",
        env=updater_env(fake_bin, LATEST_OVERRIDE="4.1.0"),
    )

    assert result.returncode != 0
    assert "Unsupported argument 'policyengine-us'" in result.stderr


def test_update_policyengine_package_dry_run_reports_planned_changes_without_editing(
    fake_bin: Path, fake_repo: Path, tmp_path: Path
) -> None:
    git_log = tmp_path / "git.log"
    install_fake_git(fake_bin, root=fake_repo, log=git_log)
    pyproject = fake_repo / "simulation" / "pyproject.toml"
    original_pyproject = pyproject.read_text(encoding="utf-8")

    result = run_updater(
        "--dry-run",
        env=updater_env(fake_bin, LATEST_OVERRIDE="4.1.0"),
    )

    assert result.returncode == 0, result.stderr
    assert "Update available: 4.0.0 -> 4.1.0" in result.stdout
    assert "Dry run: would create auto/update-policyengine-4.1.0" in result.stdout
    assert "simulation/pyproject.toml" in result.stdout
    assert "simulation/uv.lock" in result.stdout
    assert pyproject.read_text(encoding="utf-8") == original_pyproject


def test_update_policyengine_package_dry_run_reports_existing_branch_recovery(
    fake_bin: Path, fake_repo: Path, tmp_path: Path
) -> None:
    git_log = tmp_path / "git.log"
    install_fake_git(
        fake_bin,
        root=fake_repo,
        log=git_log,
        remote_branch_exists=True,
    )

    result = run_updater(
        "--dry-run",
        env=updater_env(fake_bin, LATEST_OVERRIDE="4.1.0"),
    )

    assert result.returncode == 0, result.stderr
    assert (
        "remote branch 'auto/update-policyengine-4.1.0' already exists; "
        "would ensure a PR exists for it."
    ) in result.stdout


def test_update_policyengine_package_skips_when_open_pr_exists(
    fake_bin: Path, fake_repo: Path, tmp_path: Path
) -> None:
    git_log = tmp_path / "git.log"
    gh_log = tmp_path / "gh.log"
    install_fake_git(fake_bin, root=fake_repo, log=git_log)
    install_fake_gh(fake_bin, log=gh_log, open_pr="123")

    result = run_updater(
        env=updater_env(fake_bin, LATEST_OVERRIDE="4.1.0"),
    )

    assert result.returncode == 0, result.stderr
    assert "PR #123 already exists for auto/update-policyengine-4.1.0" in result.stdout
    assert "pr create" not in gh_log.read_text(encoding="utf-8")


def test_update_policyengine_package_opens_pr_for_existing_branch_without_open_pr(
    fake_bin: Path, fake_repo: Path, tmp_path: Path
) -> None:
    git_log = tmp_path / "git.log"
    gh_log = tmp_path / "gh.log"
    install_fake_git(
        fake_bin,
        root=fake_repo,
        log=git_log,
        remote_branch_exists=True,
    )
    install_fake_gh(fake_bin, log=gh_log)

    result = run_updater(
        env=updater_env(fake_bin, LATEST_OVERRIDE="4.1.0"),
    )

    assert result.returncode == 0, result.stderr
    assert "already exists without an open PR. Creating PR." in result.stdout
    gh_calls = gh_log.read_text(encoding="utf-8")
    assert "pr list" in gh_calls
    assert "pr create" in gh_calls
    assert "--head auto/update-policyengine-4.1.0" in gh_calls


def test_update_policyengine_package_updates_py_and_bundled_runtime_pins(
    fake_bin: Path, fake_repo: Path, tmp_path: Path
) -> None:
    git_log = tmp_path / "git.log"
    gh_log = tmp_path / "gh.log"
    uv_log = tmp_path / "uv.log"
    install_fake_git(fake_bin, root=fake_repo, log=git_log, diff_has_changes=True)
    install_fake_gh(fake_bin, log=gh_log)
    install_fake_uv(fake_bin, log=uv_log)

    result = run_updater(
        env=updater_env(fake_bin, LATEST_OVERRIDE="4.1.0"),
    )

    assert result.returncode == 0, result.stderr
    assert "PR created for policyengine 4.0.0 -> 4.1.0" in result.stdout

    # The relock must be followed by the image-requirements re-export, or
    # the freshness test fails on the bot's PR.
    assert (fake_repo / "scripts" / "export.log").exists()

    pyproject_text = (fake_repo / "simulation" / "pyproject.toml").read_text(
        encoding="utf-8"
    )
    assert "policyengine==4.1.0" in pyproject_text
    assert "policyengine-core==999.999.999" in pyproject_text
    assert "policyengine-us==1.1.0" in pyproject_text
    assert "policyengine-uk==2.1.0" in pyproject_text
    uv_calls = uv_log.read_text(encoding="utf-8")
    assert "lock --upgrade-package policyengine" in uv_calls
    assert "run python -m src.modal.utils.extract_bundle_versions --shell" in uv_calls
    assert "uv lock" in uv_calls
    assert "checkout -b auto/update-policyengine-4.1.0" in git_log.read_text(
        encoding="utf-8"
    )
    assert "pr create" in gh_log.read_text(encoding="utf-8")
