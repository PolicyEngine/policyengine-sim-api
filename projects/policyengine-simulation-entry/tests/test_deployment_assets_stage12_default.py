"""Tests for moving the Stage 12 v2 default only after a deployment's checks.

The Stage 12 manifest job publishes the release's v2 worker before the
checks and leaves the manifest's default version alone. A later job, after
every check and the Cloud Run promotion, moves the default, checks that a
reader resolves it, and puts it back if a later step fails or is cancelled.
This is Stage 12's own mechanism, separate from the v1 routing jobs.
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

WORKER_JOB = "publish_stage12_v2_manifest"
DEFAULT_JOB = "publish_stage12_v2_default"
CHECK_JOBS = ("integration", "authenticated_test", "promote_entrypoint")
V1_ROUTING_CLI = "src.modal.utils.update_version_registry"
V2_MANIFEST_CLI = "src.modal.utils.update_v2_version_manifest"


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


def _context(**overrides):
    context = routing_context()
    context["inputs"]["deploy_stage12_v2"] = True
    context["needs"][WORKER_JOB] = {"result": "success"}
    for path, value in overrides.items():
        target = context
        *parents, last = path.split("__")
        for part in parents:
            target = target[part]
        target[last] = value
    return context


def test_the_worker_is_published_before_the_checks_without_moving_the_default():
    jobs = _jobs()
    publish = _step(jobs[WORKER_JOB], "Publish only the separate v2 manifest")

    assert V2_MANIFEST_CLI in publish["run"]
    assert "--keep-default" in publish["run"]
    assert "--force-default" not in publish["run"]
    # A rerun after a failed check or default move never republishes it.
    assert not _upstream(jobs, WORKER_JOB) & {*CHECK_JOBS, DEFAULT_JOB}
    assert WORKER_JOB in _upstream(jobs, "integration")


def test_the_default_moves_only_after_every_check_and_the_promotion():
    default = _jobs()[DEFAULT_JOB]

    assert (
        default["name"]
        == "Move the Stage 12 v2 default to the release after every check"
    )
    assert {"prepare", WORKER_JOB, *CHECK_JOBS} <= set(default["needs"])
    assert default["environment"] == "${{ inputs.release_environment }}"
    assert default["defaults"]["run"]["shell"] == "bash"


@pytest.mark.parametrize(
    ("overrides", "runs"),
    [
        ({}, True),
        ({"needs__integration__result": "failure"}, False),
        ({"needs__authenticated_test__result": "failure"}, False),
        ({"needs__promote_entrypoint__result": "failure"}, False),
        ({f"needs__{WORKER_JOB}__result": "failure"}, False),
        (
            {
                "inputs__promote_entrypoint": False,
                "needs__promote_entrypoint__result": "skipped",
            },
            True,
        ),
        ({"needs__promote_entrypoint__result": "skipped"}, False),
        (
            {
                "inputs__deploy_stage12_v2": False,
                f"needs__{WORKER_JOB}__result": "skipped",
            },
            False,
        ),
        # The input alone decides, even if the worker job had succeeded.
        ({"inputs__deploy_stage12_v2": False}, False),
        # A Stage 12-only deployment (stage12-deploy.yml) moves it too.
        (
            {
                "inputs__deploy_existing_stack": False,
                "needs__update_routing__result": "skipped",
            },
            True,
        ),
        ({"cancelled": True}, False),
    ],
    ids=[
        "all-pass",
        "integration-failed",
        "authenticated-tests-failed",
        "promotion-failed",
        "worker-publication-failed",
        "no-promotion-requested",
        "promotion-skipped",
        "stage12-not-requested",
        "stage12-input-off",
        "stage12-only",
        "cancelled",
    ],
)
def test_the_default_job_runs_only_when_everything_before_it_passed(overrides, runs):
    default = _jobs()[DEFAULT_JOB]

    assert (
        bool(
            evaluate_github_expression(
                default["if"], _context(**overrides), job_if=True
            )
        )
        is runs
    )


def test_the_default_job_moves_keeps_checks_and_restores_in_order():
    default = _jobs()[DEFAULT_JOB]
    order = [
        "Install Stage 12 manifest publisher dependencies",
        "Move the v2 default to the release",
        "Keep the v2 default plan",
        "Verify a reader resolves the v2 default",
        "Put the v2 default back after a failed or cancelled step",
    ]

    assert [
        step.get("name") for step in default["steps"] if step.get("name") in order
    ] == order
    move = _step(default, "Move the v2 default to the release")
    assert move["id"] == "move"
    assert "stage12-v2-default-move.sh" in move["run"]
    assert "--move-default" in step_shell(move)
    assert move["env"]["POLICYENGINE_VERSION"] == (
        "${{ needs.prepare.outputs.policyengine_version }}"
    )
    assert move["env"]["MODAL_ENVIRONMENT"] == "${{ inputs.modal_environment }}"
    keep = _step(default, "Keep the v2 default plan")
    assert keep["uses"].startswith("actions/upload-artifact@")
    assert keep["with"]["name"] == (
        "stage12-v2-default-plan-${{ inputs.release_environment }}-"
        "${{ github.run_attempt }}"
    )
    assert keep["with"]["if-no-files-found"] == "error"
    verify = _step(default, "Verify a reader resolves the v2 default")
    assert "--verify-plan" in step_shell(verify)
    restore = _step(default, "Put the v2 default back after a failed or cancelled step")
    assert default["steps"][-1] is restore
    assert restore["if"] == (
        "${{ (failure() || cancelled()) && steps.move.outcome != 'skipped' }}"
    )
    assert "--restore-plan" in step_shell(restore)


def test_the_stage12_default_shares_nothing_with_v1_routing():
    jobs = _jobs()

    for step in jobs[DEFAULT_JOB]["steps"]:
        assert V1_ROUTING_CLI not in step_shell(step)
    for job_id in ("update_routing", "publish_latest", "finalize_routing"):
        for step in jobs[job_id]["steps"]:
            assert V2_MANIFEST_CLI not in step_shell(step)


def test_the_deployment_reports_success_only_after_the_stage12_default_moved():
    ready = _jobs()["deployment_ready"]
    check = _step(ready, "Check the Stage 12 v2 default result")

    assert DEFAULT_JOB in ready["needs"]
    assert check["run"] == ".github/scripts/stage12-v2-default-ready.sh"
    assert check["env"] == {
        "REQUESTED": "${{ inputs.deploy_stage12_v2 }}",
        "DEFAULT_RESULT": f"${{{{ needs.{DEFAULT_JOB}.result }}}}",
    }


MODAL_ACCESS = {
    "MODAL_TOKEN_ID": "${{ secrets.MODAL_TOKEN_ID }}",
    "MODAL_TOKEN_SECRET": "${{ secrets.MODAL_TOKEN_SECRET }}",
    "MODAL_ENVIRONMENT": "${{ inputs.modal_environment }}",
}


@pytest.mark.parametrize(
    ("step_name", "env"),
    [
        (
            "Move the v2 default to the release",
            {
                **MODAL_ACCESS,
                "POLICYENGINE_VERSION": (
                    "${{ needs.prepare.outputs.policyengine_version }}"
                ),
            },
        ),
        ("Verify a reader resolves the v2 default", MODAL_ACCESS),
        ("Put the v2 default back after a failed or cancelled step", MODAL_ACCESS),
    ],
    ids=["move", "verify", "restore"],
)
def test_each_default_step_gets_the_modal_access_its_script_reads(step_name, env):
    step = _step(_jobs()[DEFAULT_JOB], step_name)

    assert step["env"] == env
    assert step["working-directory"] == "projects/policyengine-simulation-executor"
