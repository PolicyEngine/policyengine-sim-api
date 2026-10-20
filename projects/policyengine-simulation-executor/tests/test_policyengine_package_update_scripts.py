"""Unit tests for policyengine package updater scripts."""

from __future__ import annotations

import subprocess
import sys
from typing import TYPE_CHECKING

import pytest
from fixtures.test_policyengine_package_update_scripts import (
    SCRIPT,
    BundledVersions,
    FakeGitState,
    install_fake_gh,
    install_fake_git,
    install_fake_uv,
    run_updater,
    updater_env,
)

if TYPE_CHECKING:
    from pathlib import Path

pytest_plugins = ("fixtures.test_policyengine_package_update_scripts",)


def test_update_policyengine_package_script_has_valid_bash_syntax() -> None:
    result = subprocess.run(
        ["bash", "-n", str(SCRIPT)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_update_policyengine_package_uses_a_separate_python_helper() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    helper = SCRIPT.with_name("update_policyengine_package.py")

    assert "update_policyengine_package.py" in source
    assert "python3 -c" not in source
    assert "python -c" not in source
    assert "<<'PY'" not in source
    result = subprocess.run(
        [sys.executable, str(helper), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_update_policyengine_workflow_runs_the_tested_script() -> None:
    workflow = (
        SCRIPT.parent.parent / "workflows" / "check-policyengine-updates.yml"
    ).read_text(encoding="utf-8")

    assert SCRIPT.name == "update-policyengine-package.sh"
    assert "run: bash .github/scripts/update-policyengine-package.sh" in workflow


def test_update_policyengine_workflow_requests_issue_and_pr_permissions() -> None:
    workflow = (
        SCRIPT.parent.parent / "workflows" / "check-policyengine-updates.yml"
    ).read_text(encoding="utf-8")

    assert "issues: write" in workflow
    assert "permission-contents: write" in workflow
    assert "permission-issues: write" in workflow
    assert "permission-pull-requests: write" in workflow


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
        state=FakeGitState(remote_branch_exists=True),
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
    gh_calls = gh_log.read_text(encoding="utf-8")
    assert "pr create" not in gh_calls
    assert "issue create" not in gh_calls


def test_update_policyengine_package_opens_pr_for_existing_branch_without_open_pr(
    fake_bin: Path, fake_repo: Path, tmp_path: Path
) -> None:
    git_log = tmp_path / "git.log"
    gh_log = tmp_path / "gh.log"
    uv_log = tmp_path / "uv.log"
    install_fake_git(
        fake_bin,
        root=fake_repo,
        log=git_log,
        state=FakeGitState(remote_branch_exists=True),
    )
    install_fake_gh(fake_bin, log=gh_log)
    install_fake_uv(fake_bin, log=uv_log)

    result = run_updater(
        env=updater_env(fake_bin, LATEST_OVERRIDE="4.1.0"),
    )

    assert result.returncode == 0, result.stderr
    assert "already exists without an open PR. Verifying it." in result.stdout
    assert "Creating PR for verified branch" in result.stdout
    assert "bundle-versions" in uv_log.read_text(encoding="utf-8")
    assert "show origin/auto/update-policyengine-4.1.0:simulation/pyproject.toml" in (
        git_log.read_text(encoding="utf-8")
    )
    gh_calls = gh_log.read_text(encoding="utf-8")
    assert "pr view" in gh_calls
    assert "pr create --draft --repo PolicyEngine/policyengine-sim-api" in gh_calls
    assert "--head auto/update-policyengine-4.1.0" in gh_calls
    assert "pr-body-first-line Fixes #679" in gh_calls


@pytest.mark.parametrize(
    "recovery_case",
    [
        (
            BundledVersions(uk_version="2.0.9"),
            "4.1.0",
            '{"version":"4.1.0","bundle":{"uk":{"model_version":"2.1.0"}}}',
            "does not match the release dispatch",
        ),
        (
            BundledVersions(),
            "4.1.0",
            '{"version":"4.1.0","qualification":{"uk":{"verdict":"rejected",'
            '"promotable":false}}}',
            "does not match the release dispatch",
        ),
        (
            BundledVersions(policyengine_version="4.0.9"),
            "4.1.0",
            "",
            "reports bundle 4.0.9, expected 4.1.0",
        ),
        (BundledVersions(), "4.0.5", "", "pins policyengine 4.0.5, expected 4.1.0"),
    ],
    ids=["bundle-mismatch", "rejected-verdict", "wrong-wrapper", "stale-branch"],
)
def test_a_recovered_branch_gets_no_pr_without_verification(
    fake_bin: Path,
    fake_repo: Path,
    tmp_path: Path,
    recovery_case: tuple[BundledVersions, str, str, str],
) -> None:
    """#743 round-2 review: branch recovery may not bypass validation.

    ``recovery_case`` is the bundle the fake ``uv`` reports, the policyengine
    pin on the recovered branch, the release payload and the expected error.
    """
    bundled, branch_pin, payload, message = recovery_case
    gh_log = tmp_path / "gh.log"
    install_fake_git(
        fake_bin,
        root=fake_repo,
        log=tmp_path / "git.log",
        state=FakeGitState(remote_branch_exists=True, branch_pin=branch_pin),
    )
    install_fake_gh(fake_bin, log=gh_log)
    install_fake_uv(fake_bin, log=tmp_path / "uv.log", bundled=bundled)

    result = run_updater(
        env=updater_env(fake_bin, LATEST_OVERRIDE="4.1.0", RELEASE_PAYLOAD=payload),
    )

    assert result.returncode != 0
    assert message in result.stderr
    gh_calls = gh_log.read_text(encoding="utf-8") if gh_log.exists() else ""
    assert "pr create" not in gh_calls
    assert "issue create" not in gh_calls


def test_update_policyengine_package_updates_only_wrapper_requirements(
    fake_bin: Path, fake_repo: Path, tmp_path: Path
) -> None:
    git_log = tmp_path / "git.log"
    gh_log = tmp_path / "gh.log"
    uv_log = tmp_path / "uv.log"
    install_fake_git(
        fake_bin,
        root=fake_repo,
        log=git_log,
        state=FakeGitState(diff_has_changes=True),
    )
    install_fake_gh(fake_bin, log=gh_log)
    install_fake_uv(fake_bin, log=uv_log)

    result = run_updater(
        env=updater_env(fake_bin, LATEST_OVERRIDE="4.1.0"),
    )

    assert result.returncode == 0, result.stderr
    assert "PR created for policyengine 4.0.0 -> 4.1.0" in result.stdout

    pyproject_text = (fake_repo / "simulation" / "pyproject.toml").read_text(
        encoding="utf-8"
    )
    assert pyproject_text.count("policyengine[models]==4.1.0") == 2
    assert "policyengine==4.1.0" not in pyproject_text
    assert "policyengine-core==" not in pyproject_text
    assert "policyengine-us==" not in pyproject_text
    assert "policyengine-uk==" not in pyproject_text
    assert "spm-calculator==" not in pyproject_text
    uv_calls = uv_log.read_text(encoding="utf-8")
    assert "run --isolated --no-project --with policyengine==4.1.0 python " in uv_calls
    assert "update_policyengine_package.py bundle-versions" in uv_calls
    assert "lock --upgrade-package policyengine" not in uv_calls
    assert "uv lock" in uv_calls
    assert uv_calls.index("run --isolated") < uv_calls.index("uv lock")
    assert "lock --check" in uv_calls
    assert "run --extra test pytest tests/test_bundle_version_export.py" in uv_calls
    assert "checkout -b auto/update-policyengine-4.1.0" in git_log.read_text(
        encoding="utf-8"
    )
    assert "pr create" in gh_log.read_text(encoding="utf-8")


def test_update_policyengine_package_creates_and_links_missing_issue(
    fake_bin: Path, fake_repo: Path, tmp_path: Path
) -> None:
    git_log = tmp_path / "git.log"
    gh_log = tmp_path / "gh.log"
    uv_log = tmp_path / "uv.log"
    install_fake_git(
        fake_bin,
        root=fake_repo,
        log=git_log,
        state=FakeGitState(diff_has_changes=True),
    )
    install_fake_gh(fake_bin, log=gh_log, open_issue="")
    install_fake_uv(fake_bin, log=uv_log)

    result = run_updater(
        env=updater_env(fake_bin, LATEST_OVERRIDE="4.1.0"),
    )

    assert result.returncode == 0, result.stderr
    gh_calls = gh_log.read_text(encoding="utf-8")
    assert "api --paginate --slurp" in gh_calls
    assert "issue create --repo PolicyEngine/policyengine-sim-api" in gh_calls
    assert "issue view 680 --repo PolicyEngine/policyengine-sim-api" in gh_calls
    assert "pr-body-first-line Fixes #680" in gh_calls
    assert "pr create --draft --repo PolicyEngine/policyengine-sim-api" in gh_calls


def test_update_policyengine_package_stops_when_loaded_bundle_is_not_target(
    fake_bin: Path, fake_repo: Path, tmp_path: Path
) -> None:
    git_log = tmp_path / "git.log"
    gh_log = tmp_path / "gh.log"
    uv_log = tmp_path / "uv.log"
    install_fake_git(
        fake_bin,
        root=fake_repo,
        log=git_log,
        state=FakeGitState(diff_has_changes=True),
    )
    install_fake_gh(fake_bin, log=gh_log)
    install_fake_uv(
        fake_bin,
        log=uv_log,
        bundled=BundledVersions(policyengine_version="4.0.0"),
    )

    result = run_updater(
        env=updater_env(fake_bin, LATEST_OVERRIDE="4.1.0"),
    )

    assert result.returncode != 0
    assert "reports bundle 4.0.0, expected 4.1.0" in result.stderr
    git_calls = git_log.read_text(encoding="utf-8")
    assert "git commit" not in git_calls
    assert "git push" not in git_calls
    assert "pr create" not in gh_log.read_text(encoding="utf-8")


def test_update_policyengine_package_requires_modal_models_requirement(
    fake_bin: Path, fake_repo: Path, tmp_path: Path
) -> None:
    git_log = tmp_path / "git.log"
    gh_log = tmp_path / "gh.log"
    uv_log = tmp_path / "uv.log"
    install_fake_git(
        fake_bin,
        root=fake_repo,
        log=git_log,
        state=FakeGitState(diff_has_changes=True),
    )
    install_fake_gh(fake_bin, log=gh_log)
    install_fake_uv(fake_bin, log=uv_log)
    pyproject = fake_repo / "simulation" / "pyproject.toml"
    pyproject.write_text(
        pyproject.read_text(encoding="utf-8").split("[dependency-groups]")[0],
        encoding="utf-8",
    )

    result = run_updater(
        env=updater_env(fake_bin, LATEST_OVERRIDE="4.1.0"),
    )

    assert result.returncode != 0
    assert "in dependency-groups.modal-simulation-image; found []" in result.stderr
    git_calls = git_log.read_text(encoding="utf-8")
    assert "git commit" not in git_calls
    assert "git push" not in git_calls
    assert "pr create" not in gh_log.read_text(encoding="utf-8")


def test_update_policyengine_package_rejects_direct_component_requirements(
    fake_bin: Path, fake_repo: Path, tmp_path: Path
) -> None:
    git_log = tmp_path / "git.log"
    gh_log = tmp_path / "gh.log"
    uv_log = tmp_path / "uv.log"
    install_fake_git(
        fake_bin,
        root=fake_repo,
        log=git_log,
        state=FakeGitState(diff_has_changes=True),
    )
    install_fake_gh(fake_bin, log=gh_log)
    install_fake_uv(fake_bin, log=uv_log)
    pyproject = fake_repo / "simulation" / "pyproject.toml"
    pyproject.write_text(
        pyproject.read_text(encoding="utf-8").replace(
            'dependencies = ["policyengine[models]==4.0.0"]',
            'dependencies = ["policyengine[models]==4.0.0", '
            '"policyengine-core==0.0.0"]',
        ),
        encoding="utf-8",
    )

    result = run_updater(
        env=updater_env(fake_bin, LATEST_OVERRIDE="4.1.0"),
    )

    assert result.returncode != 0
    assert (
        "Expected project.dependencies to obtain component packages from "
        "policyengine[models]"
    ) in result.stderr
    git_calls = git_log.read_text(encoding="utf-8")
    assert "git commit" not in git_calls
    assert "git push" not in git_calls
    assert "pr create" not in gh_log.read_text(encoding="utf-8")


HELPER = SCRIPT.with_name("update_policyengine_package.py")
INSTALLED = "\n".join(
    [
        "policyengine_version=4.1.0",
        "policyengine_core_version=3.33.0",
        "uk_version=2.126.8",
        "uk_data_version=policyengine-uk-data-1.58.0",
        "uk_data_package_version=1.58.0",
        "uk_dataset_sha256=" + "a" * 64,
        "uk_certificate_sha256=",
    ]
)


def _run_helper(
    command: str,
    *,
    check: bool = False,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run one command of the repository's Python update helper."""
    return subprocess.run(
        [sys.executable, str(HELPER), command],
        input=input_text,
        capture_output=True,
        text=True,
        check=check,
        env=env,
    )


def _verify(payload: str) -> subprocess.CompletedProcess[str]:
    import os

    return _run_helper(
        "verify-dispatch",
        input_text=INSTALLED,
        env={**os.environ, "RELEASE_PAYLOAD": payload},
    )


def _payload(**uk: str) -> str:
    import json

    return json.dumps(
        {
            "version": "4.1.0",
            "bundle": {
                "uk": {
                    "model_version": "2.126.8",
                    "core_version": "3.33.0",
                    "data_release_version": "1.58.0",
                    "dataset_sha256": "a" * 64,
                    "certificate_sha256": "c" * 64,
                    **uk,
                }
            },
            "qualification": {
                "uk": {"verdict": "qualified", "receipt_sha256": "e" * 64}
            },
        }
    )


def test_verify_dispatch_accepts_a_matching_bundle() -> None:
    result = _verify(_payload())

    assert result.returncode == 0, result.stderr
    assert "dispatch_verified=true" in result.stdout
    assert "uk_qualification_verdict=qualified" in result.stdout
    assert "uk_qualification_receipt_sha256=" + "e" * 64 in result.stdout


def test_verify_dispatch_accepts_legacy_and_manual_payloads() -> None:
    for payload in ("", "null", '{"version": "4.1.0"}'):
        result = _verify(payload)
        assert result.returncode == 0, result.stderr
        assert "dispatch_verified=false" in result.stdout


def test_verify_dispatch_rejects_another_bundle() -> None:
    for field, value in (
        ("model_version", "2.125.1"),
        ("core_version", "3.32.0"),
        ("data_release_version", "1.59.0"),
        ("dataset_sha256", "b" * 64),
    ):
        result = _verify(_payload(**{field: value}))
        assert result.returncode != 0, field
        assert field in result.stderr


def test_verify_dispatch_compares_the_certificate_when_installed_reports_one() -> None:
    """#743 round-2 review: removing the certificate comparison must fail a test."""
    import os

    installed = INSTALLED.replace(
        "uk_certificate_sha256=", "uk_certificate_sha256=" + "c" * 64
    )

    def verify(payload: str) -> subprocess.CompletedProcess[str]:
        return _run_helper(
            "verify-dispatch",
            input_text=installed,
            env={**os.environ, "RELEASE_PAYLOAD": payload},
        )

    assert verify(_payload()).returncode == 0
    result = verify(_payload(certificate_sha256="d" * 64))
    assert result.returncode != 0
    assert "certificate_sha256: dispatch " + "d" * 64 in result.stderr


def test_verify_dispatch_fails_closed_when_the_installed_bundle_cannot_answer() -> None:
    """Review M3: a named dataset digest needs an installed one to compare."""
    import os

    installed = INSTALLED.replace("uk_dataset_sha256=" + "a" * 64, "uk_dataset_sha256=")
    result = _run_helper(
        "verify-dispatch",
        input_text=installed,
        env={**os.environ, "RELEASE_PAYLOAD": _payload()},
    )
    assert result.returncode != 0
    assert "dataset_sha256: dispatch" in result.stderr
    assert "installed bundle reports none" in result.stderr


def test_verify_dispatch_is_verified_only_when_something_was_compared() -> None:
    import json

    only_certificate = json.dumps(
        {"version": "4.1.0", "bundle": {"uk": {"certificate_sha256": "c" * 64}}}
    )
    unknown_only = json.dumps({"version": "4.1.0", "bundle": {"uk": {"x": "y"}}})
    for payload in (only_certificate, unknown_only):
        result = _verify(payload)
        assert result.returncode == 0, result.stderr
        assert "dispatch_verified=false" in result.stdout


def test_verify_dispatch_compares_digests_case_insensitively() -> None:
    result = _verify(_payload(dataset_sha256="A" * 64))

    assert result.returncode == 0, result.stderr
    assert "dispatch_verified=true" in result.stdout


@pytest.mark.parametrize(
    "qualification",
    [
        {"verdict": "rejected"},
        {"verdict": "incomplete"},
        {"verdict": "needs_review", "promotable": False},
        {"verdict": "qualified\ndispatch_verified=true"},
        {"verdict": "qualified", "receipt_sha256": "not-a-digest"},
    ],
    ids=["rejected", "incomplete", "unreviewed", "newline", "receipt"],
)
def test_verify_dispatch_refuses_unpromotable_or_malformed_qualifications(
    qualification,
) -> None:
    import json

    payload = json.loads(_payload())
    payload["qualification"] = {"uk": qualification}

    result = _verify(json.dumps(payload))

    assert result.returncode != 0
    assert result.stdout.count("dispatch_verified=") == 0


def test_verify_dispatch_accepts_a_reviewed_pair() -> None:
    import json

    payload = json.loads(_payload())
    payload["qualification"] = {
        "uk": {
            "verdict": "needs_review",
            "promotable": True,
            "receipt_sha256": "e" * 64,
        }
    }

    result = _verify(json.dumps(payload))

    assert result.returncode == 0, result.stderr
    assert "uk_qualification_verdict=needs_review" in result.stdout


def test_bundle_versions_matches_policyengine_release_identity() -> None:
    """Differential: the installed bundle, read two ways, must agree.

    bundle-versions (this helper) and policyengine.py's
    provenance.uk_qualification.release_bundle_identity derive the dispatch
    fields from the same manifest. The second derivation is restated here from
    that function (model_package.version, data_package.version,
    certified_data_artifact.sha256, packages.policyengine-core.version), so a
    real dispatch for the locked wrapper verifies.
    """
    import json
    import os

    from policyengine.bundle import get_current_bundle

    bundle = get_current_bundle()
    record = bundle["data_releases"]["uk"]
    output = _run_helper(
        "bundle-versions",
        check=True,
    ).stdout
    installed = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
    assert len(installed["uk_dataset_sha256"]) == 64
    payload = {
        "version": bundle["policyengine_version"],
        "bundle": {
            "uk": {
                "model_version": record["model_package"]["version"],
                "core_version": bundle["packages"]["policyengine-core"]["version"],
                "data_release_version": record["data_package"]["version"],
                "dataset_sha256": record["certified_data_artifact"]["sha256"],
            }
        },
    }
    result = _run_helper(
        "verify-dispatch",
        input_text=output,
        env={**os.environ, "RELEASE_PAYLOAD": json.dumps(payload)},
    )
    assert result.returncode == 0, result.stderr
    assert "dispatch_verified=true" in result.stdout


def test_bundle_versions_reports_the_receipt_certificate_digest(tmp_path) -> None:
    """#743 review: a wrapper with the receipt helper reports its digest.

    The locked wrapper predates the helper, so a stand-in module provides it:
    the digest it computes from the UK record must reach the output unchanged,
    and a dispatch naming another certificate must then be refused.
    """
    import json
    import os

    stand_in = tmp_path / "stand_in"
    stand_in.mkdir()
    (stand_in / "sitecustomize.py").write_text(
        "import hashlib, json, sys, types\n"
        "module = types.ModuleType('policyengine.provenance.receipt')\n"
        "module.certificate_sha256 = lambda record: hashlib.sha256(\n"
        "    json.dumps(record, sort_keys=True).encode()).hexdigest()\n"
        "import policyengine.provenance\n"
        "sys.modules['policyengine.provenance.receipt'] = module\n"
    )
    env = {**os.environ, "PYTHONPATH": str(stand_in)}
    output = _run_helper(
        "bundle-versions",
        check=True,
        env=env,
    ).stdout
    installed = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)

    import hashlib

    from policyengine.bundle import get_current_bundle

    record = get_current_bundle()["data_releases"]["uk"]
    expected = hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()
    assert installed["uk_certificate_sha256"] == expected
    mismatch = _run_helper(
        "verify-dispatch",
        input_text=output,
        env={
            **os.environ,
            "RELEASE_PAYLOAD": json.dumps(
                {"bundle": {"uk": {"certificate_sha256": "d" * 64}}}
            ),
        },
    )
    assert mismatch.returncode != 0
    assert "certificate_sha256" in mismatch.stderr


def test_verify_dispatch_rejects_another_wrapper_version() -> None:
    import json

    payload = json.loads(_payload())
    payload["version"] = "4.2.0"

    result = _verify(json.dumps(payload))

    assert result.returncode != 0
    assert "Dispatch is for policyengine 4.2.0" in result.stderr


def test_update_policyengine_package_stops_when_dispatch_names_another_dataset(
    fake_bin: Path, fake_repo: Path, tmp_path: Path
) -> None:
    git_log = tmp_path / "git.log"
    gh_log = tmp_path / "gh.log"
    uv_log = tmp_path / "uv.log"
    install_fake_git(
        fake_bin,
        root=fake_repo,
        log=git_log,
        state=FakeGitState(diff_has_changes=True),
    )
    install_fake_gh(fake_bin, log=gh_log)
    install_fake_uv(fake_bin, log=uv_log)

    result = run_updater(
        env=updater_env(
            fake_bin,
            LATEST_OVERRIDE="4.1.0",
            RELEASE_PAYLOAD=_payload(
                dataset_sha256="b" * 64,
                model_version="2.1.0",
                core_version="999.999.999",
                data_release_version="1.20.0",
            ),
        ),
    )

    assert result.returncode != 0
    assert "does not match the release dispatch" in result.stderr
    assert "git commit" not in git_log.read_text(encoding="utf-8")
    assert "pr create" not in gh_log.read_text(encoding="utf-8")


def test_update_policyengine_package_accepts_a_matching_dispatch(
    fake_bin: Path, fake_repo: Path, tmp_path: Path
) -> None:
    git_log = tmp_path / "git.log"
    gh_log = tmp_path / "gh.log"
    uv_log = tmp_path / "uv.log"
    install_fake_git(
        fake_bin,
        root=fake_repo,
        log=git_log,
        state=FakeGitState(diff_has_changes=True),
    )
    install_fake_gh(fake_bin, log=gh_log)
    install_fake_uv(fake_bin, log=uv_log)

    result = run_updater(
        env=updater_env(
            fake_bin,
            LATEST_OVERRIDE="4.1.0",
            RELEASE_PAYLOAD=_payload(
                model_version="2.1.0",
                core_version="999.999.999",
                data_release_version="1.20.0",
            ),
        ),
    )

    assert result.returncode == 0, result.stderr
    assert "pr create" in gh_log.read_text(encoding="utf-8")


# --- Characterization: the helper's bundle-versions and verify-dispatch -------


@pytest.fixture
def helper_module():
    """Load the update helper in this process, as the shell script runs it."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("update_policyengine_package", HELPER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _synthetic_bundle(**data_releases):
    return {
        "packages": {
            "policyengine": {"version": "4.1.0"},
            "policyengine-core": {"version": "3.33.0"},
            "spm-calculator": {"version": "1.0.0"},
            "policyengine-us": {"version": "1.1.0"},
            "policyengine-uk": {"version": "2.1.0"},
        },
        "data_releases": {
            "us": {
                "version": "1.10.0",
                "default_dataset": "cps_2024",
                "datasets": {"cps_2024": {"sha256": " " + "B" * 64 + "\n"}},
            },
            "uk": {
                "data_package": {"version": "1.20.0"},
                "default_dataset": "frs_2024",
                "certified_data_artifact": {"dataset": "frs_2024", "sha256": "a" * 64},
                "datasets": {"frs_2024": {"sha256": "c" * 64}},
            },
            **data_releases,
        },
    }


def _bundle_versions(monkeypatch, capsys, helper_module, bundle, *, receipt=None):
    monkeypatch.setattr("policyengine.bundle.get_current_bundle", lambda: bundle)
    # None makes the import fail, as with a wrapper that predates the helper.
    monkeypatch.setitem(sys.modules, "policyengine.provenance.receipt", receipt)
    helper_module.bundle_versions()
    return capsys.readouterr().out.splitlines()


def test_bundle_versions_prints_every_version_and_digest_in_order(
    monkeypatch, capsys, helper_module
):
    lines = _bundle_versions(monkeypatch, capsys, helper_module, _synthetic_bundle())

    assert lines == [
        "policyengine_version=4.1.0",
        "policyengine_core_version=3.33.0",
        "spm_calculator_version=1.0.0",
        "us_version=1.1.0",
        "us_data_version=1.10.0",
        "uk_version=2.1.0",
        "uk_data_version=1.20.0",
        "uk_data_package_version=1.20.0",
        "us_dataset_sha256=" + "b" * 64,
        "uk_dataset_sha256=" + "a" * 64,
        "uk_certificate_sha256=",
    ]


@pytest.mark.parametrize(
    ("uk_release", "printed"),
    [
        (
            {
                "default_dataset": "frs_2024",
                "certified_data_artifact": {"dataset": "frs_2023", "sha256": "a" * 64},
                "datasets": {"frs_2024": {"sha256": "c" * 64}},
            },
            ["uk_data_version=2024.1", "uk_dataset_sha256=" + "c" * 64],
        ),
        (
            {
                "default_dataset": "frs_2024",
                "certified_data_artifact": {"dataset": "frs_2024", "sha256": ""},
                "datasets": {"frs_2024": {"sha256": "d" * 64}},
            },
            ["uk_data_version=2024.1", "uk_dataset_sha256=" + "d" * 64],
        ),
        (
            {"default_dataset": "frs_2024", "data_package": {"version": 7}},
            [
                "uk_data_version=2024.1",
                "uk_dataset_sha256=",
                "uk_data_package_version=",
            ],
        ),
    ],
    ids=["other-certified-dataset", "empty-certified-digest", "no-digest"],
)
def test_bundle_versions_falls_back_to_the_default_dataset_reference(
    monkeypatch, capsys, helper_module, uk_release, printed
):
    bundle = _synthetic_bundle(uk={"version": "2024.1", **uk_release})

    lines = _bundle_versions(monkeypatch, capsys, helper_module, bundle)

    for line in printed:
        assert line in lines


def test_bundle_versions_reports_the_receipt_digest_of_the_uk_record(
    monkeypatch, capsys, helper_module
):
    from types import SimpleNamespace

    digested = []

    def certificate_sha256(record):
        digested.append(record)
        return "e" * 64

    bundle = _synthetic_bundle()
    lines = _bundle_versions(
        monkeypatch,
        capsys,
        helper_module,
        bundle,
        receipt=SimpleNamespace(certificate_sha256=certificate_sha256),
    )

    assert lines[-1] == "uk_certificate_sha256=" + "e" * 64
    assert digested == [bundle["data_releases"]["uk"]]


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (
            {"packages": {"policyengine-uk": {}}},
            "Bundle has no version for policyengine-uk",
        ),
        (
            {"data_releases": {"us": {"data_package": {"version": ""}}}},
            "Bundle has no data release version for us",
        ),
        (
            {
                "data_releases": {
                    "us": {"version": "1.10.0"},
                    "uk": {
                        "data_package": {"version": "1.20.0"},
                        "default_dataset": "frs_2024",
                        "datasets": {"frs_2024": {"sha256": "not-a-digest"}},
                    },
                }
            },
            "Bundle records a malformed uk dataset SHA-256",
        ),
        (
            {
                "packages": {"policyengine-uk": {"version": ""}},
                "data_releases": {"us": {"version": ""}},
            },
            "Bundle has no data release version for us",
        ),
    ],
    ids=["package", "data-release", "digest", "first-in-output-order"],
)
def test_bundle_versions_refuses_an_incomplete_bundle(
    monkeypatch, capsys, helper_module, change, message
):
    bundle = _synthetic_bundle()
    for section, entries in change.items():
        bundle[section] = {**bundle[section], **entries}

    with pytest.raises(SystemExit) as refusal:
        _bundle_versions(monkeypatch, capsys, helper_module, bundle)

    assert refusal.value.code == message
    assert capsys.readouterr().out == ""


def _verify_in_process(monkeypatch, capsys, helper_module, payload, installed):
    import io

    monkeypatch.setenv("RELEASE_PAYLOAD", payload)
    monkeypatch.setattr(sys, "stdin", io.StringIO(installed))
    helper_module.verify_dispatch()
    return capsys.readouterr().out.splitlines()


def test_verify_dispatch_prints_the_verdict_and_receipt_lines(
    monkeypatch, capsys, helper_module
):
    lines = _verify_in_process(
        monkeypatch, capsys, helper_module, _payload(), INSTALLED
    )

    assert lines == [
        "dispatch_verified=true",
        "uk_qualification_verdict=qualified",
        "uk_qualification_receipt_sha256=" + "e" * 64,
    ]


def test_verify_dispatch_strips_installed_lines_and_claims(
    monkeypatch, capsys, helper_module
):
    # One line per installed field; a line without a key is skipped.
    installed = (
        " uk_version = 2.126.8 \n=ignored\nno-separator\npolicyengine_version=4.1.0"
    )
    payload = _payload(
        model_version=" 2.126.8 ",
        core_version="",
        data_release_version=None,
        dataset_sha256=None,
        certificate_sha256=None,
    )

    lines = _verify_in_process(monkeypatch, capsys, helper_module, payload, installed)

    assert lines[0] == "dispatch_verified=true"


def test_verify_dispatch_lowercases_only_digest_claims(
    monkeypatch, capsys, helper_module
):
    installed = INSTALLED.replace("uk_version=2.126.8", "uk_version=2.126.8a")

    with pytest.raises(SystemExit) as refusal:
        _verify_in_process(
            monkeypatch,
            capsys,
            helper_module,
            _payload(model_version="2.126.8A", dataset_sha256="A" * 64),
            installed,
        )

    assert refusal.value.code == (
        "The release dispatch names a different UK bundle from the installed "
        "wheel: model_version: dispatch 2.126.8A, installed 2.126.8a"
    )


def test_verify_dispatch_lists_every_mismatch_in_check_order(
    monkeypatch, capsys, helper_module
):
    installed = INSTALLED.replace("uk_dataset_sha256=" + "a" * 64, "uk_dataset_sha256=")

    with pytest.raises(SystemExit) as refusal:
        _verify_in_process(
            monkeypatch,
            capsys,
            helper_module,
            _payload(model_version="2.0.0", data_release_version="1.0.0"),
            installed,
        )

    assert refusal.value.code == (
        "The release dispatch names a different UK bundle from the installed "
        "wheel: model_version: dispatch 2.0.0, installed 2.126.8; "
        "data_release_version: dispatch 1.0.0, installed 1.58.0; "
        "dataset_sha256: dispatch " + "a" * 64 + ", installed bundle reports none"
    )
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    ("dispatch", "message"),
    [
        (
            {"version": "4.2.0", "bundle": {"uk": {"model_version": "0"}}},
            "Dispatch is for policyengine 4.2.0, but 4.1.0 is installed.",
        ),
        (
            {
                "bundle": {"uk": {"model_version": "0"}},
                "qualification": {"uk": {"verdict": "Rejected"}},
            },
            "The release dispatch names a different UK bundle from the installed "
            "wheel: model_version: dispatch 0, installed 2.126.8",
        ),
        (
            {"qualification": {"uk": {"verdict": 5, "receipt_sha256": 7}}},
            "The release dispatch carries a malformed verdict",
        ),
        (
            {"qualification": {"uk": {"verdict": "qualified", "receipt_sha256": 7}}},
            "The release dispatch carries a malformed receipt digest",
        ),
        (
            {"qualification": {"uk": {"promotable": False}}},
            "The release dispatch says the UK pair was not promotable ()",
        ),
        (
            {"qualification": {"uk": {"verdict": "incomplete", "promotable": True}}},
            "The release dispatch says the UK pair was not promotable (incomplete)",
        ),
    ],
    ids=[
        "wrapper-first",
        "bundle-before-qualification",
        "verdict-before-receipt",
        "receipt",
        "unpromotable-without-verdict",
        "unpromotable-verdict",
    ],
)
def test_verify_dispatch_refusals_and_their_order(
    monkeypatch, capsys, helper_module, dispatch, message
):
    import json

    with pytest.raises(SystemExit) as refusal:
        _verify_in_process(
            monkeypatch, capsys, helper_module, json.dumps(dispatch), INSTALLED
        )

    assert refusal.value.code == message


def test_verify_dispatch_without_a_payload_compares_nothing(
    monkeypatch, capsys, helper_module
):
    monkeypatch.delenv("RELEASE_PAYLOAD", raising=False)
    import io

    monkeypatch.setattr(sys, "stdin", io.StringIO(INSTALLED))
    helper_module.verify_dispatch()

    assert capsys.readouterr().out.splitlines() == [
        "dispatch_verified=false",
        "uk_qualification_verdict=",
        "uk_qualification_receipt_sha256=",
    ]
