"""The staging deploy calculates a household on the promoted revision.

The household function reports a call's end to the stable URL, which serves
the promoted revision, so the staging household job runs after promotion,
only in staging, and only when the deploy also deployed the v2 application.
"""

from __future__ import annotations

from test_deployment_assets_support import REUSABLE_DEPLOY_WORKFLOW, workflow_jobs

JOB_NAME = "staging_household_test"
TEST_FILE = "authenticated_tests/test_staging_household_calculation.py"


def _job() -> dict:
    return workflow_jobs(REUSABLE_DEPLOY_WORKFLOW)[JOB_NAME]


def test_the_job_runs_after_a_successful_staging_promotion_of_the_v2_path() -> None:
    job = _job()
    condition = " ".join(job["if"].split())

    assert "promote_entrypoint" in job["needs"]
    assert "needs.promote_entrypoint.result == 'success'" in condition
    assert "inputs.deployment_environment == 'staging'" in condition
    assert "inputs.deploy_stage12_v2" in condition
    assert "!cancelled()" in condition
    assert job["environment"] == "${{ inputs.release_environment }}"


def test_the_job_targets_the_stable_url_with_the_deployed_version() -> None:
    environment = _job()["env"]

    assert environment["SIMULATION_STAGING_HOUSEHOLD_TEST"] == "1"
    assert environment["SIMULATION_STAGING_TEST_BASE_URL"] == (
        "${{ needs.deploy_entrypoint.outputs.stable_url }}"
    )
    assert environment["SIMULATION_STAGING_TEST_POLICYENGINE_VERSION"] == (
        "${{ needs.prepare.outputs.policyengine_version }}"
    )
    assert environment["SIMULATION_STAGING_TEST_PUBLIC_API_URL"] == (
        "${{ vars.PUBLIC_API_URL }}"
    )
    assert {"deploy_entrypoint", "prepare"} <= set(_job()["needs"])


def test_the_job_authenticates_as_the_deployed_test_client() -> None:
    environment = _job()["env"]

    assert environment["SIMULATION_ENTRYPOINT_TEST_AUTH_ISSUER"] == (
        "${{ secrets.SIMULATION_ENTRYPOINT_AUTH_ISSUER }}"
    )
    assert environment["SIMULATION_ENTRYPOINT_TEST_AUTH_AUDIENCE"] == (
        "${{ secrets.SIMULATION_ENTRYPOINT_AUTH_AUDIENCE }}"
    )
    assert environment["SIMULATION_ENTRYPOINT_TEST_AUTH_CLIENT_ID"] == (
        "${{ secrets.SIMULATION_ENTRYPOINT_TEST_AUTH_CLIENT_ID }}"
    )
    assert environment["SIMULATION_ENTRYPOINT_TEST_AUTH_CLIENT_SECRET"] == (
        "${{ secrets.SIMULATION_ENTRYPOINT_TEST_AUTH_CLIENT_SECRET }}"
    )


def test_the_job_runs_only_the_staging_household_test() -> None:
    commands = [step.get("run", "") for step in _job()["steps"]]

    assert f"uv run --frozen pytest {TEST_FILE} -v" in commands
    assert all("\n" not in command.strip() for command in commands)


def test_other_runs_of_the_deployed_tests_skip_the_household_test() -> None:
    jobs = workflow_jobs(REUSABLE_DEPLOY_WORKFLOW)
    flagged = [
        name
        for name, job in jobs.items()
        if "SIMULATION_STAGING_HOUSEHOLD_TEST" in job.get("env", {})
        or any(
            "SIMULATION_STAGING_HOUSEHOLD_TEST" in step.get("env", {})
            for step in job.get("steps", [])
        )
    ]

    assert flagged == [JOB_NAME]
