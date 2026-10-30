"""Tests for publishing and finalizing routing in the deployment workflows."""

from __future__ import annotations

import json
import os
import re

import pytest
from test_deployment_assets_support import (
    REPOSITORY_ROOT,
    REUSABLE_DEPLOY_WORKFLOW,
    ROLLBACK_WORKFLOW,
    evaluate_github_expression,
    job_section,
    routing_context,
    run_bash,
    workflow_jobs,
)


def _run_routing_step_with_a_failing_command(tmp_path, step, failed_command, **env):
    """Run the real body with Actions' shell and a failing CLI seam."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "uv").write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "$*" >> "$UV_CALLS"\n'
        # Valid output ensures a masked plan/identity failure cannot be
        # caught accidentally by a later grep rather than the failed CLI.
        "printf 'before_sha256=before\\nafter_sha256=after\\nadvances_latest=true\\n'\n"
        "printf 'policyengine_version=6.3.0\\napp_name=simulation-6.3.0\\n'\n"
        "printf 'uk_model_version=2.126.8\\nuk_dataset_sha256=abc\\n'\n"
        'if [[ " $* " == *" ${FAILED_COMMAND} "* ]]; then exit 9; fi\n'
        "exit 0\n"
    )
    (bin_dir / "uv").chmod(0o755)
    (bin_dir / "gh").write_text("#!/usr/bin/env bash\nprintf '1\\n'\n")
    (bin_dir / "gh").chmod(0o755)
    runner_temp = tmp_path / "runner-temp"
    plan = runner_temp / "routing-plans" / "routing-plan-prod-1" / "plan.json"
    plan.parent.mkdir(parents=True)
    plan.write_text("{}")
    result = run_bash(
        "--noprofile",
        "--norc",
        "-eo",
        "pipefail",
        "-c",
        step["run"],
        check=False,
        capture_output=True,
        env={
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "RUNNER_TEMP": str(runner_temp),
            "GITHUB_OUTPUT": str(tmp_path / "github-output"),
            "GITHUB_RUN_ID": "744001",
            "GITHUB_RUN_ATTEMPT": "2",
            "GITHUB_REPOSITORY": "PolicyEngine/policyengine-sim-api",
            "UV_CALLS": str(tmp_path / "uv-calls"),
            "FAILED_COMMAND": failed_command,
            "MODAL_ENVIRONMENT": "main",
            "PUBLICATION_RUN": "744001",
            "RELEASE_ENVIRONMENT": "prod",
            "GATES_PASSED": "true",
            "APP_NAME": "simulation-6.3.0",
            "POLICYENGINE_VERSION": "6.3.0",
            "POLICYENGINE_US_VERSION": "1.0.0",
            "POLICYENGINE_UK_VERSION": "2.126.8",
            "FORCE_LATEST": "false",
            "TARGET": "prod",
            "DEPLOY_RUN_ID": "",
            "EXPECTED_LATEST": "",
            "RESOLVE_PENDING_PUBLICATION": "false",
            **env,
        },
    )
    assert result.returncode == 9, result.stderr
    assert failed_command in (tmp_path / "uv-calls").read_text()


@pytest.mark.parametrize(
    ("workflow", "job_name", "command", "env"),
    [
        (REUSABLE_DEPLOY_WORKFLOW, "update_routing", "sync", {}),
        (REUSABLE_DEPLOY_WORKFLOW, "finalize_routing", "sync", {}),
        (ROLLBACK_WORKFLOW, "rollback", "sync", {}),
        (REUSABLE_DEPLOY_WORKFLOW, "update_routing", "--plan-out", {}),
        (REUSABLE_DEPLOY_WORKFLOW, "update_routing", "--record-plan", {}),
        (REUSABLE_DEPLOY_WORKFLOW, "update_routing", "--apply-plan", {}),
        (REUSABLE_DEPLOY_WORKFLOW, "finalize_routing", "--finalize-publication", {}),
        (
            ROLLBACK_WORKFLOW,
            "rollback",
            "--restore-plan",
            {"DEPLOY_RUN_ID": "744001"},
        ),
        (ROLLBACK_WORKFLOW, "rollback", "--restore-latest", {}),
        (ROLLBACK_WORKFLOW, "rollback", "--print-identity", {}),
        (
            ROLLBACK_WORKFLOW,
            "rollback",
            "--print-pending-plan",
            {"RESOLVE_PENDING_PUBLICATION": "true"},
        ),
        (
            ROLLBACK_WORKFLOW,
            "rollback",
            "--finalize-publication",
            {"RESOLVE_PENDING_PUBLICATION": "true"},
        ),
    ],
    ids=[
        "publication-setup",
        "finalizer-setup",
        "manual-rollback-setup",
        "plan",
        "record",
        "apply",
        "finalize",
        "manual-restore-plans",
        "manual-restore-latest",
        "manual-read-identity",
        "operator-inspect-pending",
        "operator-resolve-pending",
    ],
)
def test_a_failing_routing_command_fails_its_step_and_job(
    tmp_path, workflow, job_name, command, env
):
    job = workflow_jobs(workflow)[job_name]
    assert "continue-on-error" not in job
    assert job["defaults"]["run"]["shell"] == "bash"
    step = next(step for step in job["steps"] if command in step.get("run", ""))
    assert "continue-on-error" not in step
    assert step.get("shell", "bash") == "bash"
    _run_routing_step_with_a_failing_command(tmp_path, step, command, **env)


@pytest.mark.parametrize(
    ("workflow", "job_name"),
    [
        (REUSABLE_DEPLOY_WORKFLOW, "update_routing"),
        (REUSABLE_DEPLOY_WORKFLOW, "finalize_routing"),
        (REUSABLE_DEPLOY_WORKFLOW, "deployment_ready"),
        (ROLLBACK_WORKFLOW, "rollback"),
    ],
)
def test_routing_jobs_never_allow_command_failures(workflow, job_name):
    job = workflow_jobs(workflow)[job_name]
    assert "continue-on-error" not in job
    assert job.get("defaults", {}).get("run", {}).get("shell") == "bash"
    for step in job["steps"]:
        assert "continue-on-error" not in step
        assert step.get("shell", "bash") == "bash"
        body = step.get("run", "")
        assert not re.search(r"\|\|\s*true\b", body)
        assert not re.search(r"\bset\s+\+e\b", body)


def test_each_publication_is_named_by_its_run_attempt_and_environment():
    routing = workflow_jobs(REUSABLE_DEPLOY_WORKFLOW)["update_routing"]
    plan = next(
        step
        for step in routing["steps"]
        if step.get("name") == "Plan the routing change"
    )
    assert (
        '--publication-id "${GITHUB_RUN_ID}-'
        '${GITHUB_RUN_ATTEMPT}-${RELEASE_ENVIRONMENT}"' in plan["run"]
    )
    assert plan["env"]["RELEASE_ENVIRONMENT"] == "${{ inputs.release_environment }}"


def test_publication_keeps_the_plan_but_finalization_reads_the_journal():
    jobs = workflow_jobs(REUSABLE_DEPLOY_WORKFLOW)
    routing = jobs["update_routing"]
    names = [step.get("name") for step in routing["steps"]]
    plan = names.index("Plan the routing change")
    journal = names.index("Record the unfinished routing publication")
    keep = names.index("Keep the plan for a guarded restore")
    publish = names.index("Publish the planned routing")
    assert "Record the plan artifact" not in names
    # The plan is kept before anything is published.
    assert plan < journal < keep < publish
    assert "--record-plan" in routing["steps"][journal]["run"]
    keep_step = routing["steps"][keep]
    assert keep_step["uses"].startswith("actions/upload-artifact@")
    assert keep_step["with"]["if-no-files-found"] == "error"
    assert keep_step["with"]["name"] == (
        "routing-plan-${{ inputs.release_environment }}-${{ github.run_attempt }}"
    )
    assert "--plan-out" in routing["steps"][plan]["run"]
    assert "--apply-plan" in routing["steps"][publish]["run"]
    assert routing["outputs"] == {
        "before_sha256": "${{ steps.plan.outputs.before_sha256 }}",
        "after_sha256": "${{ steps.plan.outputs.after_sha256 }}",
        "advances_latest": "${{ steps.plan.outputs.advances_latest }}",
    }

    assert "revert_routing" not in jobs
    finalizer = jobs["finalize_routing"]
    finalize = next(
        step
        for step in finalizer["steps"]
        if "--finalize-publication" in step.get("run", "")
    )
    assert not any(
        step.get("uses", "").startswith("actions/download-artifact@")
        for step in finalizer["steps"]
    )
    assert '--publication-run "${PUBLICATION_RUN}"' in finalize["run"]
    assert '--release-environment "${RELEASE_ENVIRONMENT}"' in finalize["run"]
    assert '--gates-passed "${GATES_PASSED}"' in finalize["run"]
    assert '--github-output "$GITHUB_OUTPUT"' in finalize["run"]
    assert finalize["env"]["PUBLICATION_RUN"] == "${{ github.run_id }}"
    assert finalize["env"]["RELEASE_ENVIRONMENT"] == (
        "${{ inputs.release_environment }}"
    )
    assert "github.run_attempt" not in json.dumps(finalizer)
    assert finalizer["outputs"]["outcome"] == ("${{ steps.finalize.outputs.outcome }}")
    assert finalize["id"] == "finalize"

    cleanup = job_section(
        REUSABLE_DEPLOY_WORKFLOW.read_text(encoding="utf-8"),
        "cleanup_entrypoint_revisions",
    )
    assert (
        "SIMULATION_ENTRYPOINT_ROLLBACK_REVISION: "
        "${{ needs.deploy_entrypoint.outputs.previous_revision }}"
    ) in cleanup


def test_deployment_ready_requires_the_journal_finalizer_to_complete():
    jobs = workflow_jobs(REUSABLE_DEPLOY_WORKFLOW)
    ready = jobs["deployment_ready"]
    assert {
        "update_routing",
        "integration",
        "authenticated_test",
        "promote_entrypoint",
        "finalize_routing",
    } <= set(ready["needs"])
    finalizer = jobs["finalize_routing"]
    assert {
        "update_routing",
        "integration",
        "authenticated_test",
        "promote_entrypoint",
    } <= set(finalizer["needs"])
    gate = next(
        step for step in ready["steps"] if step.get("name") == "Check deployment result"
    )
    assert "continue-on-error" not in gate
    assert gate["env"]["EXISTING_STACK_REQUESTED"] == (
        "${{ inputs.deploy_existing_stack }}"
    )
    assert gate["env"]["ROUTING_RESULT"] == "${{ needs.update_routing.result }}"
    assert gate["env"]["FINALIZE_RESULT"] == "${{ needs.finalize_routing.result }}"
    assert gate["env"]["FINALIZE_OUTCOME"] == (
        "${{ needs.finalize_routing.outputs.outcome }}"
    )
    assert not any(
        "--complete-plan" in step.get("run", "")
        or step.get("uses", "").startswith("actions/download-artifact@")
        for step in ready["steps"]
    )


@pytest.mark.parametrize(
    "failed_gate",
    [
        "ENTRYPOINT_RESULT",
        "INTEGRATION_RESULT",
        "AUTHENTICATED_TEST_RESULT",
        "PROMOTION_RESULT",
        "ROUTING_RESULT",
        "FINALIZE_RESULT",
        "FINALIZE_OUTCOME",
    ],
)
def test_failed_release_gate_cannot_report_a_successful_release(tmp_path, failed_gate):
    ready = workflow_jobs(REUSABLE_DEPLOY_WORKFLOW)["deployment_ready"]
    gate = next(
        step for step in ready["steps"] if step.get("name") == "Check deployment result"
    )
    marker = tmp_path / "completed"
    env = {
        "PATH": os.environ["PATH"],
        "PROMOTION_REQUESTED": "true",
        "ENTRYPOINT_RESULT": "success",
        "INTEGRATION_RESULT": "success",
        "AUTHENTICATED_TEST_RESULT": "success",
        "PROMOTION_RESULT": "success",
        "EXISTING_STACK_REQUESTED": "true",
        "ROUTING_RESULT": "success",
        "FINALIZE_RESULT": "success",
        "FINALIZE_OUTCOME": "completed",
    }
    env[failed_gate] = "failure"
    # A later step cannot run after this gate under Actions' bash -e behavior.
    result = run_bash(
        "-eo",
        "pipefail",
        "-c",
        gate["run"] + '\ntouch "$1"',
        "gate",
        str(marker),
        check=False,
        capture_output=True,
        env=env,
        cwd=REPOSITORY_ROOT,
    )
    assert result.returncode != 0
    assert not marker.exists()


@pytest.mark.parametrize(
    ("existing", "result", "outcome", "passes"),
    [
        ("true", "success", "completed", True),
        ("true", "success", "restored", False),
        ("true", "success", "no-pending", False),
        ("true", "success", "foreign-publication", False),
        ("true", "failure", "completed", False),
        ("true", "cancelled", "", False),
        ("true", "skipped", "", False),
        ("false", "skipped", "", True),
    ],
)
def test_release_success_requires_completed_finalization_for_published_routing(
    existing, result, outcome, passes
):
    gate = next(
        step
        for step in workflow_jobs(REUSABLE_DEPLOY_WORKFLOW)["deployment_ready"]["steps"]
        if step.get("name") == "Check deployment result"
    )
    completed = run_bash(
        "-eo",
        "pipefail",
        "-c",
        gate["run"],
        check=False,
        capture_output=True,
        cwd=REPOSITORY_ROOT,
        env={
            "PATH": os.environ["PATH"],
            "PROMOTION_REQUESTED": "false",
            "ENTRYPOINT_RESULT": "success",
            "INTEGRATION_RESULT": "success",
            "AUTHENTICATED_TEST_RESULT": "success",
            "PROMOTION_RESULT": "skipped",
            "EXISTING_STACK_REQUESTED": existing,
            "ROUTING_RESULT": "success" if existing == "true" else "skipped",
            "FINALIZE_RESULT": result,
            "FINALIZE_OUTCOME": outcome,
        },
    )
    assert (completed.returncode == 0) is passes, completed.stderr


@pytest.mark.parametrize(
    ("existing", "advances", "expected"),
    [(True, "true", "true"), (True, "false", ""), (False, "true", ""), (True, "", "")],
)
def test_latest_is_expected_only_when_the_release_advances_it(
    existing, advances, expected
):
    jobs = workflow_jobs(REUSABLE_DEPLOY_WORKFLOW)
    smoke_step = next(
        step
        for step in jobs["integration"]["steps"]
        if "cloud-run-simulation-entry-smoke.sh" in step.get("run", "")
    )
    context = routing_context(
        inputs__deploy_existing_stack=existing,
        needs__update_routing__outputs__advances_latest=advances,
    )
    for environment in (smoke_step["env"], jobs["promote_entrypoint"]["env"]):
        assert (
            evaluate_github_expression(environment["EXPECTED_LATEST"], context)
            == expected
        )
