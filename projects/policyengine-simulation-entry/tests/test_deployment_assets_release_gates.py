"""Tests for the expression reader, and the release checks that finalize routing."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest
from test_deployment_assets_support import (
    IDENTITY,
    REUSABLE_DEPLOY_WORKFLOW,
    VERSIONS_BODY,
    evaluate_github_expression,
    routing_context,
    run_bash,
    run_identity_smoke,
    sentinel_check_step,
    workflow_jobs,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_the_expression_evaluator_follows_github_semantics():
    context = {"inputs": {"flag": True}, "needs": {"a": {"result": "failure"}}}
    assert evaluate_github_expression("${{ inputs.flag == 'true' }}", context) is False
    assert evaluate_github_expression("${{ inputs.flag == true }}", context) is True
    assert evaluate_github_expression("${{ 'SUCCESS' == 'success' }}", context) is True
    assert evaluate_github_expression("${{ inputs.missing == '' }}", context) is True
    # No status function: implicitly success() first, so a failed need skips it.
    assert not evaluate_github_expression("${{ inputs.flag }}", context, job_if=True)
    assert evaluate_github_expression(
        "${{ always() && inputs.flag }}", context, job_if=True
    )
    assert not evaluate_github_expression(
        "${{ !cancelled() }}", {**context, "cancelled": True}
    )


def test_the_expression_evaluator_returns_operands_and_job_statuses():
    context = {
        "inputs": {"name": "beta", "empty": "", "zero": 0, "flag": False},
        "needs": {"a": {"result": "success"}, "b": {"result": "skipped"}},
        "steps": "not-a-mapping",
    }
    # || and && return one of their operands, as GitHub does, not a boolean.
    assert (
        evaluate_github_expression("${{ inputs.empty || 'fallback' }}", context)
        == "fallback"
    )
    assert (
        evaluate_github_expression("${{ inputs.name || 'fallback' }}", context)
        == "beta"
    )
    assert evaluate_github_expression("${{ inputs.name && inputs.zero }}", context) == 0
    assert (
        evaluate_github_expression("${{ inputs.flag && inputs.name }}", context)
        is False
    )
    assert evaluate_github_expression("${{ inputs.name != 'BETA' }}", context) is False
    assert (
        evaluate_github_expression("${{ !(inputs.empty || inputs.zero) }}", context)
        is True
    )
    assert evaluate_github_expression("${{ steps.x.outputs }}", context) is None
    assert evaluate_github_expression("${{ inputs.missing.deeper }}", context) is None
    assert evaluate_github_expression("false", context) is False
    # success() needs every needed job to succeed; failure() needs one failure.
    assert evaluate_github_expression("${{ success() }}", context) is False
    assert evaluate_github_expression("${{ failure() }}", context) is False
    assert evaluate_github_expression("${{ success() }}", {}) is True
    assert evaluate_github_expression("${{ success() }}", {"cancelled": True}) is False
    assert evaluate_github_expression("${{ cancelled() }}", {"cancelled": True}) is True
    # Any status function in a job if replaces the implicit success().
    assert (
        evaluate_github_expression("${{ inputs.name == 'beta' }}", context, job_if=True)
        is False
    )
    assert (
        evaluate_github_expression(
            "${{ inputs.name == 'beta' || failure() }}", context, job_if=True
        )
        is True
    )


@pytest.mark.parametrize(
    "expression",
    ["${{ unknown() }}", "${{ inputs.name inputs.name }}", "${{ inputs.name @ }}"],
    ids=["unknown-function", "trailing-token", "unknown-character"],
)
def test_the_expression_evaluator_refuses_what_it_cannot_read(expression):
    with pytest.raises(AssertionError):
        evaluate_github_expression(expression, {"inputs": {"name": "beta"}})


@pytest.mark.parametrize(
    ("overrides", "finalizes", "gates_passed"),
    [
        ({}, True, "true"),
        ({"needs__integration__result": "failure"}, True, "false"),
        ({"needs__authenticated_test__result": "cancelled"}, True, "false"),
        ({"needs__promote_entrypoint__result": "failure"}, True, "false"),
        # Publication then a failure inside update_routing itself (#744 H2).
        ({"needs__update_routing__result": "failure"}, True, "false"),
        (
            {
                "inputs__promote_entrypoint": False,
                "needs__promote_entrypoint__result": "skipped",
            },
            True,
            "true",
        ),
        # Missing job outputs cannot prove nothing was published: runner loss
        # can prevent reporting them even after upload and apply succeeded.
        (
            {
                "needs__update_routing__result": "failure",
                "needs__update_routing__outputs": {},
            },
            True,
            "false",
        ),
        (
            {
                "needs__update_routing__result": "failure",
                "needs__update_routing__outputs": {},
                "needs__integration__result": "skipped",
                "needs__authenticated_test__result": "skipped",
                "needs__promote_entrypoint__result": "skipped",
            },
            True,
            "false",
        ),
        # The restore does not depend on the plan's digests: even a plan that
        # reported equal ones must be restored, because the restore is what
        # resolves the release's publication record, which would otherwise
        # refuse every later deployment.
        (
            {
                "needs__integration__result": "failure",
                "needs__update_routing__outputs__after_sha256": "a" * 64,
            },
            True,
            "false",
        ),
        (
            {
                "inputs__deploy_existing_stack": False,
                "needs__integration__result": "failure",
            },
            False,
            "false",
        ),
        (
            {"needs__update_routing__result": "skipped"},
            False,
            "false",
        ),
        # Stage12 alone never publishes, even when all its gates pass.
        (
            {
                "inputs__deploy_existing_stack": False,
                "needs__update_routing__result": "skipped",
            },
            False,
            "false",
        ),
        # Re-run failed jobs carries a successful attempt-1 publication and
        # its outputs. Finalization must use the run, never attempt-2 artifacts.
        (
            {"github__run_attempt": "2"},
            True,
            "true",
        ),
        (
            {
                "github__run_attempt": "2",
                "needs__authenticated_test__result": "failure",
            },
            True,
            "false",
        ),
        # A cancelled run after publication must still restore routing.
        (
            {
                "cancelled": True,
                "needs__integration__result": "cancelled",
                "needs__authenticated_test__result": "skipped",
                "needs__promote_entrypoint__result": "skipped",
            },
            True,
            "false",
        ),
    ],
    ids=[
        "all-pass",
        "integration",
        "authenticated",
        "promotion",
        "routing-job",
        "no-promotion",
        "missing-outputs",
        "runner-lost-all-outputs",
        "equal-digests",
        "stack-disabled",
        "routing-skipped",
        "stage12-only",
        "attempt-2-success",
        "attempt-2-failed-gate",
        "cancelled",
    ],
)
def test_finalizer_always_resolves_a_publication_with_the_release_gate_outcome(
    overrides, finalizes, gates_passed
):
    finalizer = workflow_jobs(REUSABLE_DEPLOY_WORKFLOW)["finalize_routing"]
    step = next(
        step
        for step in finalizer["steps"]
        if "--finalize-publication" in step.get("run", "")
    )
    context = routing_context(**overrides)
    assert (
        bool(evaluate_github_expression(finalizer["if"], context, job_if=True))
        is finalizes
    )
    assert str(
        evaluate_github_expression(step["env"]["GATES_PASSED"], context)
    ).lower() == (gates_passed)


@pytest.mark.parametrize("job", ["integration", "promote_entrypoint"])
def test_a_failing_sentinel_fails_its_step(tmp_path, job):
    """#744 review: `|| true` on the sentinel command must not pass a failure."""
    step = sentinel_check_step(workflow_jobs(REUSABLE_DEPLOY_WORKFLOW)[job])
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "uv").write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$*" == *"release_sentinel check"* ]]; then exit 9; fi\n'
        "exit 0\n"
    )
    (bin_dir / "uv").chmod(0o755)
    script = step["run"].replace(
        "${{ needs.deploy_entrypoint.outputs.candidate_url }}", "https://candidate"
    )
    result = run_bash(
        # GitHub runs `run` blocks with bash -e -o pipefail.
        "--noprofile",
        "--norc",
        "-eo",
        "pipefail",
        "-c",
        script,
        check=False,
        capture_output=True,
        env={
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "RUNNER_TEMP": str(tmp_path),
            "STABLE_URL": "https://stable",
        },
    )
    assert result.returncode == 9


def test_stage12_only_promotion_runs_whatever_the_skipped_stack_jobs():
    """#743 review: the explicit status function must stay active."""
    promote = workflow_jobs(REUSABLE_DEPLOY_WORKFLOW)["promote_entrypoint"]
    context = {
        "inputs": {"promote_entrypoint": True, "deploy_existing_stack": False},
        "needs": {
            "prepare": {"result": "success"},
            "deploy_entrypoint": {"result": "success"},
            "deploy_executor": {"result": "skipped"},
            "authenticated_test": {"result": "success"},
            "sentinel_expected": {"result": "skipped"},
            "update_routing": {"result": "skipped"},
        },
    }
    assert evaluate_github_expression(promote["if"], context, job_if=True)
    context["needs"]["authenticated_test"]["result"] = "failure"
    assert not evaluate_github_expression(promote["if"], context, job_if=True)
    context["needs"]["authenticated_test"]["result"] = "success"
    context["inputs"]["promote_entrypoint"] = False
    assert not evaluate_github_expression(promote["if"], context, job_if=True)


def test_smoke_rejects_a_latest_wrapper_that_lags_alone(tmp_path: Path):
    """#743 review: the wrapper's latest is checked, not only the model's."""
    import copy

    versions = copy.deepcopy(VERSIONS_BODY)
    versions["policyengine"]["latest"] = "6.2.6"

    result = run_identity_smoke(tmp_path, versions, **IDENTITY)

    assert result.returncode == 1
    assert "latest routes to policyengine 6.2.6 and policyengine-uk 2.126.8" in (
        result.stderr
    )
