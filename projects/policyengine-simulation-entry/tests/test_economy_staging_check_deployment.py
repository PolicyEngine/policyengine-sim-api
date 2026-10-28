"""The staging check of a small v2 economy report, after each staging promotion."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
REUSABLE_DEPLOY_WORKFLOW = (
    REPOSITORY_ROOT / ".github" / "workflows" / "simulation-deploy.reusable.yml"
)
CHECK = "authenticated_tests/test_staging_economy_report.py"


def _job() -> dict:
    jobs = yaml.safe_load(REUSABLE_DEPLOY_WORKFLOW.read_text(encoding="utf-8"))["jobs"]
    return jobs["economy_staging_check"]


def test_the_check_runs_on_staging_once_the_tested_revision_is_promoted() -> None:
    job = _job()

    assert set(job["needs"]) >= {"prepare", "deploy_entrypoint", "promote_entrypoint"}
    condition = " ".join(job["if"].split())
    assert "inputs.deployment_environment == 'staging'" in condition
    assert "needs.promote_entrypoint.result == 'success'" in condition
    assert job["environment"] == "${{ inputs.release_environment }}"


def test_the_check_submits_to_the_stable_url_with_the_test_client() -> None:
    environment = _job()["env"]

    assert environment["SIMULATION_ENTRYPOINT_TEST_BASE_URL"] == (
        "${{ needs.deploy_entrypoint.outputs.stable_url }}"
    )
    assert environment["SIMULATION_STAGING_ECONOMY_POLICYENGINE_VERSION"] == (
        "${{ needs.prepare.outputs.policyengine_version }}"
    )
    assert environment["SIMULATION_STAGING_ECONOMY_CHECK"] == "1"
    assert {
        name: value
        for name, value in environment.items()
        if name.startswith("SIMULATION_ENTRYPOINT_TEST_AUTH_")
    } == {
        "SIMULATION_ENTRYPOINT_TEST_AUTH_ISSUER": (
            "${{ secrets.SIMULATION_ENTRYPOINT_AUTH_ISSUER }}"
        ),
        "SIMULATION_ENTRYPOINT_TEST_AUTH_AUDIENCE": (
            "${{ secrets.SIMULATION_ENTRYPOINT_AUTH_AUDIENCE }}"
        ),
        "SIMULATION_ENTRYPOINT_TEST_AUTH_CLIENT_ID": (
            "${{ secrets.SIMULATION_ENTRYPOINT_TEST_AUTH_CLIENT_ID }}"
        ),
        "SIMULATION_ENTRYPOINT_TEST_AUTH_CLIENT_SECRET": (
            "${{ secrets.SIMULATION_ENTRYPOINT_TEST_AUTH_CLIENT_SECRET }}"
        ),
    }


def test_the_check_runs_only_its_own_test_and_never_stops_a_deployment() -> None:
    job = _job()

    # The v2 economy path serves no user yet; its failure is reported in the
    # run without holding back the production deployment.
    assert job["continue-on-error"] is True
    assert job["timeout-minutes"] <= 30
    commands = [step["run"] for step in job["steps"] if "run" in step]
    assert f"uv run --frozen pytest {CHECK} -v" in commands


def test_the_check_is_skipped_wherever_it_is_not_asked_for() -> None:
    """The deployed-auth runs collect the whole directory; only this job asks."""
    environment = {
        name: value
        for name, value in os.environ.items()
        if name != "SIMULATION_STAGING_ECONOMY_CHECK"
    }

    result = subprocess.run(
        [sys.executable, "-m", "pytest", CHECK, "-q", "-rs", "-p", "no:cacheprovider"],
        cwd=REPOSITORY_ROOT / "projects" / "policyengine-simulation-entry",
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout
    assert "1 skipped" in result.stdout


def test_the_staging_deployment_promotes_so_the_check_runs() -> None:
    """The check runs after the promotion, which every staging caller asks for."""
    for workflow in ("simulation-deploy.yml", "stage12-deploy.yml"):
        path = REPOSITORY_ROOT / ".github" / "workflows" / workflow
        jobs = yaml.safe_load(path.read_text(encoding="utf-8"))["jobs"]
        staging = [
            job
            for job in jobs.values()
            if job.get("with", {}).get("deployment_environment") == "staging"
        ]
        assert staging, workflow
        assert all(job["with"]["promote_entrypoint"] is True for job in staging)


def _staging_check_module():
    import importlib.util

    path = REPOSITORY_ROOT / "projects" / "policyengine-simulation-entry" / CHECK
    spec = importlib.util.spec_from_file_location("staging_economy_check", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_check_runs_again_only_when_the_submission_reused_finished_runs() -> None:
    check = _staging_check_module()

    def submitted(status: str) -> dict:
        return {"run": {"baseline_run": {"status": status}}}

    # A submission that calculates its own run already uses this
    # deployment's functions; one that reused finished runs does not.
    assert check.needs_fresh_run(submitted("running")) is False
    assert check.needs_fresh_run(submitted("pending")) is False
    assert check.needs_fresh_run(submitted("succeeded")) is True
