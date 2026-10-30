"""Tests for promotion order, Stage 12-only deployments and the sentinel."""

from __future__ import annotations

import copy
import re

from test_deployment_assets_support import (
    DEPLOY_WORKFLOW,
    DEPLOYMENT_READY_SCRIPT,
    REUSABLE_DEPLOY_WORKFLOW,
    REUSABLE_DEPLOY_WORKFLOW_REFERENCE,
    STAGE12_DEPLOY_WORKFLOW,
    STAGING_PROMOTION_FIXTURES,
    WORKFLOWS_DIRECTORY,
    evaluate_github_expression,
    job_section,
    sentinel_check_step,
    workflow_jobs,
)


def test_full_stack_promotion_order_is_explicit():
    deploy_workflow = DEPLOY_WORKFLOW.read_text(encoding="utf-8")
    reusable_workflow = REUSABLE_DEPLOY_WORKFLOW.read_text(encoding="utf-8")

    for deployment_step, unit_test_step in (
        ("Deploy Cloud Run entrypoint candidate", "Run entrypoint unit tests"),
        ("Deploy stable Modal gateway", "Run gateway unit tests"),
        ("Deploy versioned Modal executor", "Run executor unit tests"),
    ):
        assert reusable_workflow.index(deployment_step) < reusable_workflow.index(
            unit_test_step
        )

    routing_dependencies = (
        "needs: [prepare, deploy_entrypoint, deploy_gateway, deploy_executor]"
    )
    assert routing_dependencies in reusable_workflow
    gateway_job = reusable_workflow[
        reusable_workflow.index("\n  deploy_gateway:") : reusable_workflow.index(
            "\n  deploy_executor:"
        )
    ]
    assert "needs: [prepare, deploy_executor]" in gateway_job
    assert reusable_workflow.index("\n  update_routing:") < reusable_workflow.index(
        "\n  integration:"
    )
    assert (
        "stage12_v2_ready"
        in reusable_workflow[
            reusable_workflow.index("\n  integration:") : reusable_workflow.index(
                "\n  authenticated_test:"
            )
        ]
    )
    assert reusable_workflow.index("\n  integration:") < reusable_workflow.index(
        "\n  authenticated_test:"
    )
    assert reusable_workflow.index("\n  authenticated_test:") < reusable_workflow.index(
        "\n  promote_entrypoint:"
    )
    promote_needs = set(
        workflow_jobs(REUSABLE_DEPLOY_WORKFLOW)["promote_entrypoint"]["needs"]
    )
    assert {
        "prepare",
        "deploy_entrypoint",
        "deploy_executor",
        "authenticated_test",
        "sentinel_expected",
        "update_routing",
    } <= promote_needs
    assert (
        'cloud-run-simulation-entry-smoke.sh "${STABLE_URL}" "${TARGET_REVISION}" '
        '"${PREVIOUS_REVISION}"' in reusable_workflow
    )
    assert (
        'cloud-run-simulation-entry-smoke.sh "${CUSTOM_URL}" "${TARGET_REVISION}" '
        '"${PREVIOUS_REVISION}"' in reusable_workflow
    )
    assert (
        "cloud-run-simulation-entry-smoke.sh "
        '"${{ needs.deploy_entrypoint.outputs.candidate_url }}" '
        '"${{ needs.deploy_entrypoint.outputs.revision }}"' in reusable_workflow
    )
    assert "failure() && steps.promote.outcome == 'success'" in reusable_workflow
    assert "needs: beta" in deploy_workflow
    assert "skip_beta" not in deploy_workflow


def test_stage12_only_deployment_runs_candidate_tests_and_requested_promotion():
    reusable_workflow = REUSABLE_DEPLOY_WORKFLOW.read_text(encoding="utf-8")

    authenticated_test = re.search(
        r"^  authenticated_test:\n(?P<body>.*?)(?=^  [a-z0-9_-]+:\n|\Z)",
        reusable_workflow,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert authenticated_test is not None
    authenticated_test_body = authenticated_test.group("body")
    assert "!cancelled()" in authenticated_test_body
    assert "needs.deploy_entrypoint.result == 'success'" in authenticated_test_body
    assert "needs.integration.result == 'success'" in authenticated_test_body

    promotion = re.search(
        r"^  promote_entrypoint:\n(?P<body>.*?)(?=^  [a-z0-9_-]+:\n|\Z)",
        reusable_workflow,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert promotion is not None
    promotion_body = promotion.group("body")
    assert "!cancelled()" in promotion_body
    assert "inputs.promote_entrypoint" in promotion_body
    assert "needs.deploy_entrypoint.result == 'success'" in promotion_body
    assert "needs.authenticated_test.result == 'success'" in promotion_body


def test_reusable_deployment_rejects_unexpected_skipped_phases():
    reusable_workflow = REUSABLE_DEPLOY_WORKFLOW.read_text(encoding="utf-8")
    completion = re.search(
        r"^  deployment_ready:\n(?P<body>.*?)(?=^  [a-z0-9_-]+:\n|\Z)",
        reusable_workflow,
        flags=re.MULTILINE | re.DOTALL,
    )

    assert completion is not None
    completion_body = completion.group("body")
    assert "if: ${{ !cancelled() }}" in completion_body
    for dependency in (
        "deploy_entrypoint",
        "integration",
        "authenticated_test",
        "promote_entrypoint",
    ):
        assert dependency in completion_body
    assert "run: .github/scripts/simulation-deployment-ready.sh" in completion_body
    for result in (
        "ENTRYPOINT_RESULT: ${{ needs.deploy_entrypoint.result }}",
        "INTEGRATION_RESULT: ${{ needs.integration.result }}",
        "AUTHENTICATED_TEST_RESULT: ${{ needs.authenticated_test.result }}",
        "PROMOTION_RESULT: ${{ needs.promote_entrypoint.result }}",
    ):
        assert result in completion_body
    readiness_check = DEPLOYMENT_READY_SCRIPT.read_text(encoding="utf-8")
    for expected_result in (
        'test "${ENTRYPOINT_RESULT}" = "success"',
        'test "${INTEGRATION_RESULT}" = "success"',
        'test "${AUTHENTICATED_TEST_RESULT}" = "success"',
        'test "${PROMOTION_RESULT}" = "success"',
        'test "${PROMOTION_RESULT}" = "skipped"',
    ):
        assert expected_result in readiness_check


def test_complete_integration_suite_is_configured_for_beta_only():
    deploy_workflow = DEPLOY_WORKFLOW.read_text(encoding="utf-8")
    reusable_workflow = REUSABLE_DEPLOY_WORKFLOW.read_text(encoding="utf-8")

    beta_workflow = deploy_workflow[
        deploy_workflow.index("  beta:") : deploy_workflow.index("  prod:")
    ]
    prod_workflow = deploy_workflow[
        deploy_workflow.index("  prod:") : deploy_workflow.index("  summary:")
    ]

    assert "run_full_integration: true" in beta_workflow
    assert "run_full_integration: false" in prod_workflow
    assert "run_full_integration:" in reusable_workflow


def test_stage12_only_deployment_skips_existing_stack_integration_suite():
    stage12_workflow = STAGE12_DEPLOY_WORKFLOW.read_text(encoding="utf-8")

    assert stage12_workflow.count("run_full_integration: false") == 2
    assert "run_full_integration: true" not in stage12_workflow


def test_main_deployment_automatically_deploys_stage12_in_both_environments():
    deploy_workflow = DEPLOY_WORKFLOW.read_text(encoding="utf-8")
    reusable_workflow = REUSABLE_DEPLOY_WORKFLOW.read_text(encoding="utf-8")

    beta_workflow = deploy_workflow[
        deploy_workflow.index("  beta:") : deploy_workflow.index("  prod:")
    ]
    prod_workflow = deploy_workflow[
        deploy_workflow.index("  prod:") : deploy_workflow.index("  summary:")
    ]

    assert "deploy_stage12_v2: true" in beta_workflow
    assert "deploy_stage12_v2: true" in prod_workflow
    assert deploy_workflow.count("deploy_stage12_v2: true") == 2
    assert "deploy_stage12_v2: false" not in deploy_workflow

    assert "src.modal.utils.set_stage12_dual_execution" not in reusable_workflow
    assert "STAGE12_ENABLED_VALUE: ${{ vars.STAGE12_ENABLED }}" in reusable_workflow


def test_stage12_worker_deployment_can_request_a_google_oidc_token():
    reusable_workflow = REUSABLE_DEPLOY_WORKFLOW.read_text(encoding="utf-8")
    match = re.search(
        r"^  deploy_stage12_v2:\n(?P<body>.*?)(?=^  [a-z0-9_-]+:\n|\Z)",
        reusable_workflow,
        flags=re.MULTILINE | re.DOTALL,
    )

    assert match is not None
    section = match.group("body")
    assert "permissions:\n      contents: read\n      id-token: write" in section
    assert "google-github-actions/auth@v2" in section


def test_stage12_only_deployment_cannot_redeploy_existing_modal_resources():
    reusable_workflow = REUSABLE_DEPLOY_WORKFLOW.read_text(encoding="utf-8")

    for job_name in ("deploy_gateway", "deploy_executor", "update_routing"):
        match = re.search(
            rf"^  {job_name}:\n(?P<body>.*?)(?=^  [a-z0-9_-]+:\n|\Z)",
            reusable_workflow,
            flags=re.MULTILINE | re.DOTALL,
        )
        assert match is not None
        section = match.group("body")
        assert "if: ${{ inputs.deploy_existing_stack }}" in section

    stage12_section = reusable_workflow[
        reusable_workflow.index("\n  deploy_stage12_v2:") : reusable_workflow.index(
            "\n  integration:"
        )
    ]
    assert "src.modal.utils.update_version_registry" not in stage12_section
    finalizer_job = job_section(reusable_workflow, "finalize_routing")
    assert "inputs.deploy_existing_stack &&" in finalizer_job
    sentinel_job = job_section(reusable_workflow, "sentinel_expected")
    assert "if: ${{ inputs.deploy_existing_stack }}" in sentinel_job


def test_deployed_stack_must_reproduce_the_numerical_sentinel():
    workflow = REUSABLE_DEPLOY_WORKFLOW.read_text(encoding="utf-8")
    jobs = workflow_jobs(REUSABLE_DEPLOY_WORKFLOW)
    sentinel_job = job_section(workflow, "sentinel_expected")
    assert jobs["sentinel_expected"]["needs"] == "prepare"
    assert "release_sentinel expected" in sentinel_job
    assert "secrets.PE_UK_PRIVATE_HF_READ_TOKEN" in sentinel_job
    assert "secrets.GCP_CREDENTIALS_JSON" in sentinel_job
    assert ".uk.dataset_sha256 == $d" in sentinel_job

    integration = jobs["integration"]
    assert "needs.sentinel_expected.result == 'success'" in integration["if"]
    assert "needs.sentinel_expected.result == 'skipped'" in integration["if"]
    check = sentinel_check_step(integration)
    # Both checks run on every existing-stack deployment, after the identity
    # smoke, against the candidate and then the stable URL.
    assert check["if"] == "${{ inputs.deploy_existing_stack }}"
    assert "needs.deploy_entrypoint.outputs.candidate_url" in check["run"]
    steps = integration["steps"]
    smoke = next(
        index
        for index, step in enumerate(steps)
        if "cloud-run-simulation-entry-smoke.sh" in step.get("run", "")
    )
    assert smoke < steps.index(check)

    promote = jobs["promote_entrypoint"]
    stable = sentinel_check_step(promote)
    assert stable["if"] == "${{ inputs.deploy_existing_stack }}"
    assert '--base-url "${STABLE_URL}"' in stable["run"]
    names = [step.get("name", "") for step in promote["steps"]]
    # A failing stable sentinel triggers the existing traffic restore step.
    assert promote["steps"].index(stable) < next(
        index
        for index, name in enumerate(names)
        if name.startswith("Restore previous revision")
    )


# The promotion input every staging deployment passes: true only for a run
# from main, so a manual run from another branch never moves staging's stable
# traffic or deletes its older revisions.
MAIN_ONLY_PROMOTION = "${{ github.ref == 'refs/heads/main' }}"


def _staging_deployment_job_ids(jobs: dict) -> list[str]:
    """Return the jobs that run the reusable deployment workflow for staging."""
    return [
        job_id
        for job_id, job in jobs.items()
        if job.get("uses") == REUSABLE_DEPLOY_WORKFLOW_REFERENCE
        and (job.get("with") or {}).get("deployment_environment") == "staging"
    ]


def staging_promotion_violations(jobs: dict) -> list[str]:
    """Return each staging deployment job that doesn't promote exactly from main.

    Rule sim.deploy.staging-promotes-tested-revision: a staging deployment must
    pass ``promote_entrypoint: ${{ github.ref == 'refs/heads/main' }}`` to the
    reusable deployment workflow. Any other value counts as a violation:
    ``false`` leaves the stable URL on a revision the deployment never tested,
    and ``true`` lets a run from another branch promote an unmerged build.
    """
    return [
        f"{job_id} must pass promote_entrypoint: {MAIN_ONLY_PROMOTION}"
        for job_id in _staging_deployment_job_ids(jobs)
        if jobs[job_id]["with"].get("promote_entrypoint") != MAIN_ONLY_PROMOTION
    ]


def test_staging_deployments_promote_their_tested_entrypoint_revision():
    """Staging's stable URL serves the revision that passed staging's tests.

    Callers such as the Public API's staging service use the staging stable
    URL, so a staging deployment that left stable traffic on an older revision
    would leave them calling a build that the deployment did not test.
    """
    staging_jobs_by_workflow = {}
    violations = []
    for workflow_path in sorted(WORKFLOWS_DIRECTORY.glob("*.yml")):
        jobs = workflow_jobs(workflow_path)
        staging_job_ids = _staging_deployment_job_ids(jobs)
        if staging_job_ids:
            staging_jobs_by_workflow[workflow_path.name] = staging_job_ids
        violations.extend(
            f"{workflow_path.name}: {violation}"
            for violation in staging_promotion_violations(jobs)
        )

    # The check must find the two known staging deployments, so that a renamed
    # input or reusable workflow cannot make it pass by finding nothing.
    assert "beta" in staging_jobs_by_workflow.get(DEPLOY_WORKFLOW.name, [])
    assert "deploy-stage12-staging" in staging_jobs_by_workflow.get(
        STAGE12_DEPLOY_WORKFLOW.name, []
    )
    for workflow_name, staging_job_ids in staging_jobs_by_workflow.items():
        jobs = workflow_jobs(WORKFLOWS_DIRECTORY / workflow_name)
        for job_id in staging_job_ids:
            assert (
                jobs[job_id]["with"]["entrypoint_service"]
                == "policyengine-simulation-entry-staging"
            )
    assert violations == []


def test_rule_proof_staging_promotes_tested_revision():
    """Proof for rule sim.deploy.staging-promotes-tested-revision.

    The bad fixture is the `beta` deployment as it was before the rule: it
    tested a tagged candidate but never moved staging's stable traffic to it.
    Passing a literal ``true`` is also a violation, since a run from another
    branch would then promote that branch's build.
    """
    bad_jobs = workflow_jobs(STAGING_PROMOTION_FIXTURES / "bad_simulation_deploy.yml")
    good_jobs = workflow_jobs(STAGING_PROMOTION_FIXTURES / "good_simulation_deploy.yml")
    any_branch_jobs = copy.deepcopy(good_jobs)
    any_branch_jobs["beta"]["with"]["promote_entrypoint"] = True

    violation = f"beta must pass promote_entrypoint: {MAIN_ONLY_PROMOTION}"
    assert staging_promotion_violations(bad_jobs) == [violation]
    assert staging_promotion_violations(any_branch_jobs) == [violation]
    assert staging_promotion_violations(good_jobs) == []
    # The required value is true for a run from main and false for any other
    # branch or for a tag.
    for ref, promotes in (
        ("refs/heads/main", True),
        ("refs/heads/feat/stage12-segmentation-cache", False),
        ("refs/heads/main-backport", False),
        ("refs/tags/v1.0.0", False),
    ):
        context = {"github": {"ref": ref}}
        assert evaluate_github_expression(MAIN_ONLY_PROMOTION, context) is promotes
