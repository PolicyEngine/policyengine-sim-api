"""Tests for moving ``latest`` only after a deployment's checks and promotion.

The routes job publishes the release's routes before the checks, which pin
the release's versions, and leaves ``latest`` alone. The latest job runs
after every check and the Cloud Run promotion, moves ``latest`` when the
release is newer, checks that unpinned requests reach the release, and puts
``latest`` back if a step after the move fails.
"""

from __future__ import annotations

import pytest
from test_deployment_assets_support import (
    REUSABLE_DEPLOY_WORKFLOW,
    evaluate_github_expression,
    routing_context,
    step_shell,
    workflow_jobs,
)

ROUTES_JOB = "update_routing"
LATEST_JOB = "publish_latest"
CHECK_JOBS = ("integration", "authenticated_test", "promote_entrypoint")
SMOKE = "cloud-run-simulation-entry-smoke.sh"


def _jobs() -> dict:
    return workflow_jobs(REUSABLE_DEPLOY_WORKFLOW)


def _step(job: dict, name: str) -> dict:
    matching = [step for step in job["steps"] if step.get("name") == name]
    assert len(matching) == 1, name
    return matching[0]


def _upstream(jobs: dict, job_id: str) -> set[str]:
    needs = jobs[job_id].get("needs", [])
    needs = [needs] if isinstance(needs, str) else needs
    found = set(needs)
    for needed in needs:
        found |= _upstream(jobs, needed)
    return found


def test_latest_moves_only_after_every_check_and_the_promotion():
    jobs = _jobs()
    latest = jobs[LATEST_JOB]

    assert latest["name"] == "Move latest to the release after every check"
    assert {ROUTES_JOB, *CHECK_JOBS} <= set(latest["needs"])
    assert latest["environment"] == "${{ inputs.release_environment }}"
    assert latest["defaults"]["run"]["shell"] == "bash"


@pytest.mark.parametrize(
    ("overrides", "runs"),
    [
        ({}, True),
        ({"needs__integration__result": "failure"}, False),
        ({"needs__authenticated_test__result": "failure"}, False),
        ({"needs__promote_entrypoint__result": "failure"}, False),
        ({"needs__update_routing__result": "failure"}, False),
        (
            {
                "inputs__promote_entrypoint": False,
                "needs__promote_entrypoint__result": "skipped",
            },
            True,
        ),
        # Promotion was requested, so a skipped promotion is not a pass.
        ({"needs__promote_entrypoint__result": "skipped"}, False),
        # A Stage 12-only deployment publishes no routing at all.
        (
            {
                "inputs__deploy_existing_stack": False,
                "needs__update_routing__result": "skipped",
            },
            False,
        ),
        ({"cancelled": True}, False),
    ],
    ids=[
        "all-pass",
        "integration-failed",
        "authenticated-tests-failed",
        "promotion-failed",
        "routes-failed",
        "no-promotion-requested",
        "promotion-skipped",
        "stage12-only",
        "cancelled",
    ],
)
def test_the_latest_job_runs_only_when_everything_before_it_passed(overrides, runs):
    latest = _jobs()[LATEST_JOB]
    context = routing_context(**overrides)

    assert bool(evaluate_github_expression(latest["if"], context, job_if=True)) is runs


def test_a_rerun_of_the_failed_jobs_never_reruns_the_routes_job():
    """GitHub reruns a failed job and every job that needs it.

    No check or latest job is upstream of the routes job, so a rerun after a
    failed check, or a failed latest move, leaves the published routes alone.
    """
    jobs = _jobs()

    assert not _upstream(jobs, ROUTES_JOB) & {*CHECK_JOBS, LATEST_JOB}
    assert ROUTES_JOB in _upstream(jobs, LATEST_JOB)
    for check in CHECK_JOBS:
        assert ROUTES_JOB in _upstream(jobs, check) | set(jobs[check].get("needs", []))


def test_the_routes_job_completes_its_publication_after_publishing():
    routes = _jobs()[ROUTES_JOB]
    names = [step.get("name") for step in routes["steps"]]

    publish = names.index("Publish the planned routing")
    complete = names.index("Complete the routing publication")
    assert complete == publish + 1 == len(names) - 1
    assert "routing-complete-plan.sh" in routes["steps"][complete]["run"]
    assert "--complete-plan" in step_shell(routes["steps"][complete])
    # The routes plan never moves latest, so it takes no --force-latest.
    plan = _step(routes, "Plan the routing change")
    assert "FORCE_LATEST" not in plan["env"]
    assert "advances_latest" not in routes["outputs"]


def test_the_latest_job_plans_records_keeps_moves_checks_and_completes_in_order():
    latest = _jobs()[LATEST_JOB]
    names = [step.get("name") for step in latest["steps"]]
    order = [
        "Install routing publisher dependencies",
        "Plan moving latest to the release",
        "Record the unfinished latest publication",
        "Keep the latest plan for a guarded restore",
        "Move latest to the release",
        "Verify unpinned requests reach the release",
        "Complete the latest publication",
        "Put latest back after a failed step",
    ]

    assert [name for name in names if name in order] == order
    plan = _step(latest, "Plan moving latest to the release")
    assert plan["id"] == "plan"
    assert "routing-plan-latest.sh" in plan["run"]
    assert "--plan-latest-out" in step_shell(plan)
    assert plan["env"]["FORCE_LATEST"] == "${{ inputs.force_latest }}"
    assert plan["env"]["RELEASE_ENVIRONMENT"] == "${{ inputs.release_environment }}"
    assert plan["env"]["APP_NAME"] == "${{ needs.deploy_executor.outputs.app_name }}"
    record = _step(latest, "Record the unfinished latest publication")
    assert record["id"] == "record"
    assert "--record-plan" in step_shell(record)
    keep = _step(latest, "Keep the latest plan for a guarded restore")
    assert keep["uses"].startswith("actions/upload-artifact@")
    assert keep["with"]["name"] == (
        "routing-latest-plan-${{ inputs.release_environment }}-"
        "${{ github.run_attempt }}"
    )
    assert keep["with"]["if-no-files-found"] == "error"
    assert "--apply-plan" in step_shell(_step(latest, "Move latest to the release"))
    assert "--complete-plan" in step_shell(
        _step(latest, "Complete the latest publication")
    )
    assert latest["outputs"] == {
        "advances_latest": "${{ steps.plan.outputs.advances_latest }}",
    }


def test_the_latest_job_puts_latest_back_when_a_step_after_recording_fails():
    latest = _jobs()[LATEST_JOB]
    restore = _step(latest, "Put latest back after a failed step")

    assert latest["steps"][-1] is restore
    assert restore["if"] == "${{ failure() && steps.record.outcome == 'success' }}"
    assert "routing-restore-plan.sh" in restore["run"]
    assert "--restore-plan" in step_shell(restore)


@pytest.mark.parametrize(
    ("advances", "expected_latest"),
    [("true", "true"), ("false", ""), ("", "")],
)
def test_the_latest_check_expects_latest_only_when_the_release_advances_it(
    advances, expected_latest
):
    verify = _step(_jobs()[LATEST_JOB], "Verify unpinned requests reach the release")
    context = routing_context()
    context["steps"] = {"plan": {"outputs": {"advances_latest": advances}}}

    assert SMOKE in verify["run"]
    assert (
        evaluate_github_expression(verify["env"]["EXPECTED_LATEST"], context)
        == expected_latest
    )
    assert verify["env"]["EXPECTED_IDENTITY"] == "required"
    assert verify["env"]["EXPECTED_APP_NAME"] == (
        "${{ needs.deploy_executor.outputs.app_name }}"
    )
    assert verify["env"]["EXPECTED_POLICYENGINE_VERSION"] == (
        "${{ needs.prepare.outputs.policyengine_version }}"
    )
    assert verify["env"]["EXPECTED_UK_MODEL_VERSION"] == (
        "${{ needs.prepare.outputs.uk_version }}"
    )
    assert verify["env"]["EXPECTED_UK_DATASET_SHA256"] == (
        "${{ needs.prepare.outputs.uk_dataset_sha256 }}"
    )


@pytest.mark.parametrize(
    ("promote", "url", "previous"),
    [
        (True, "https://stable.example.test", "previous-revision"),
        (False, "https://candidate.example.test", ""),
    ],
    ids=["promoted-stable-url", "unpromoted-candidate-url"],
)
def test_the_latest_check_reads_the_url_callers_use(promote, url, previous):
    verify = _step(_jobs()[LATEST_JOB], "Verify unpinned requests reach the release")
    context = routing_context(inputs__promote_entrypoint=promote)
    context["needs"]["deploy_entrypoint"] = {
        "outputs": {
            "stable_url": "https://stable.example.test",
            "candidate_url": "https://candidate.example.test",
            "revision": "target-revision",
            "previous_revision": "previous-revision",
        }
    }

    assert evaluate_github_expression(verify["env"]["VERIFY_URL"], context) == url
    assert (
        evaluate_github_expression(verify["env"]["PREVIOUS_REVISION"], context)
        == previous
    )
    assert evaluate_github_expression(verify["env"]["TARGET_REVISION"], context) == (
        "target-revision"
    )
    assert verify["run"] == (
        f'.github/scripts/{SMOKE} "${{VERIFY_URL}}" '
        '"${TARGET_REVISION}" "${PREVIOUS_REVISION}"'
    )


def test_the_checks_before_latest_moves_do_not_expect_latest():
    jobs = _jobs()
    smoke_steps = [
        step
        for job in ("integration", "promote_entrypoint")
        for step in jobs[job]["steps"]
        if SMOKE in step.get("run", "")
    ]

    assert smoke_steps
    for step in smoke_steps:
        assert "EXPECTED_LATEST" not in step.get("env", {})
    assert "EXPECTED_LATEST" not in jobs["promote_entrypoint"]["env"]


@pytest.mark.parametrize(
    ("promote", "custom_url", "runs"),
    [
        (True, "https://simulation.example.org", True),
        (True, "", False),
        (False, "https://simulation.example.org", False),
    ],
    ids=["promoted-with-custom-hostname", "no-custom-hostname", "not-promoted"],
)
def test_the_latest_check_also_reads_a_configured_custom_hostname(
    promote, custom_url, runs
):
    latest = _jobs()[LATEST_JOB]
    verify = _step(latest, "Verify unpinned requests reach the release")
    custom = _step(
        latest, "Verify unpinned requests reach the release through the custom hostname"
    )
    context = routing_context(inputs__promote_entrypoint=promote)
    context["vars"] = {"SIMULATION_ENTRYPOINT_CUSTOM_URL": custom_url}

    names = [step.get("name") for step in latest["steps"]]
    assert (
        names.index(verify["name"])
        < names.index(custom["name"])
        < names.index("Complete the latest publication")
    )
    assert bool(evaluate_github_expression(custom["if"], context)) is runs
    assert custom["env"]["CUSTOM_URL"] == "${{ vars.SIMULATION_ENTRYPOINT_CUSTOM_URL }}"
    assert custom["run"] == (
        f'.github/scripts/{SMOKE} "${{CUSTOM_URL}}" '
        '"${TARGET_REVISION}" "${PREVIOUS_REVISION}"'
    )
    # The same identity and latest expectation as the stable URL's check.
    assert {
        key: value for key, value in custom["env"].items() if key != "CUSTOM_URL"
    } == {key: value for key, value in verify["env"].items() if key != "VERIFY_URL"}
