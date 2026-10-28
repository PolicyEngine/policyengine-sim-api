"""The staging deploy calculates a household on the promoted revision.

The household function reports a call's end to the stable URL, which serves
the promoted revision, so the staging household job runs after promotion,
only in staging, and only when the deploy also deployed the v2 application.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import httpx
import pytest
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


def _staging_test_module():
    """Load the staging household test module, which the deployed job runs."""
    path = Path(__file__).resolve().parents[1] / TEST_FILE
    specification = importlib.util.spec_from_file_location(
        "staging_household_calculation", path
    )
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def test_the_submission_names_the_stored_household_by_id_only() -> None:
    """Sim reads the household, its policy and the catalog version itself."""
    module = _staging_test_module()

    assert module.submission_body("household-id", "6.10.0") == {
        "country": "us",
        "policyengine_version": "6.10.0",
        "household_id": "household-id",
        "force": True,
    }


class ScriptedAnswers:
    """Answer each request with the next scripted status and body."""

    def __init__(self, answers: list[tuple[int, dict]]) -> None:
        """Keep the answers in order."""
        self.answers = list(answers)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        """Return the next answer."""
        self.requests.append(request)
        status, body = self.answers.pop(0)
        headers = (
            {"Retry-After": "1"} if status == httpx.codes.SERVICE_UNAVAILABLE else {}
        )
        return httpx.Response(status, json=body, headers=headers)


def test_polling_waits_out_a_service_that_asks_to_retry(monkeypatch) -> None:
    module = _staging_test_module()
    answers = ScriptedAnswers(
        [
            (503, {"detail": "The simulation database could not be reached."}),
            (200, {"status": "running"}),
            (200, {"status": "succeeded", "result": {}}),
        ]
    )
    waits: list[float] = []
    monkeypatch.setattr(module.time, "sleep", waits.append)
    monkeypatch.setattr(
        module.httpx, "get", httpx.Client(transport=httpx.MockTransport(answers)).get
    )

    finished = module.poll_until_finished(
        "https://stable.example", "id", {}, deadline=module.time.monotonic() + 60
    )

    assert finished["status"] == "succeeded"
    assert len(answers.requests) == 3
    assert waits == [1.0, module.POLL_INTERVAL_SECONDS]


def test_a_retry_that_would_pass_the_deadline_returns_the_refusal(
    monkeypatch,
) -> None:
    module = _staging_test_module()
    answers = ScriptedAnswers([(503, {"detail": "unavailable"})])
    monkeypatch.setattr(module.time, "sleep", pytest.fail)

    response = module.answer_within(
        lambda: httpx.Client(transport=httpx.MockTransport(answers)).get(
            "https://stable.example/v2/household-calculations/id"
        ),
        deadline=module.time.monotonic() + 0.5,
    )

    assert response.status_code == httpx.codes.SERVICE_UNAVAILABLE
    assert len(answers.requests) == 1
