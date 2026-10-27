"""Tests for script syntax, the stable smoke, Stage 12 schema use and gcloud."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from test_deployment_assets_support import (
    AUTHENTICATED_TESTS,
    CLOUD_RUN_DEPLOY_SCRIPT,
    DEPLOY_WORKFLOW,
    REPOSITORY_ROOT,
    REUSABLE_DEPLOY_WORKFLOW,
    REUSABLE_DEPLOY_WORKFLOW_REFERENCE,
    SMOKE_SCRIPT,
    STAGE12_DEPLOY_WORKFLOW,
    STAGE12_VALIDATION_SCRIPT,
    STAGING_PROMOTION_FIXTURES,
    TRAFFIC_SCRIPT,
    WORKFLOWS_DIRECTORY,
    run_bash,
    smoke_test_env,
    workflow_jobs,
    write_fake_smoke_curl,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_deployment_scripts_have_valid_shell_syntax():
    run_bash("-n", TRAFFIC_SCRIPT, check=True)
    run_bash("-n", SMOKE_SCRIPT, check=True)
    run_bash("-n", CLOUD_RUN_DEPLOY_SCRIPT, check=True)
    assert os.access(SMOKE_SCRIPT, os.X_OK)
    assert os.access(CLOUD_RUN_DEPLOY_SCRIPT, os.X_OK)


def test_stable_smoke_retries_the_previous_revision_until_target_is_served(
    tmp_path: Path,
):
    old_revision = "policyengine-simulation-entry-00001-old"
    target_revision = "policyengine-simulation-entry-00002-new"
    state_file, revisions_file = write_fake_smoke_curl(
        tmp_path,
        [old_revision, old_revision, target_revision],
    )

    run_bash(
        SMOKE_SCRIPT,
        "https://simulation.example.test",
        target_revision,
        old_revision,
        check=True,
        capture_output=True,
        env=smoke_test_env(tmp_path, state_file, revisions_file),
    )

    assert state_file.read_text(encoding="utf-8") == "3"


def test_stable_smoke_rejects_an_unexpected_revision_without_retrying(tmp_path: Path):
    old_revision = "policyengine-simulation-entry-00001-old"
    target_revision = "policyengine-simulation-entry-00002-new"
    unexpected_revision = "policyengine-simulation-entry-00003-unexpected"
    state_file, revisions_file = write_fake_smoke_curl(
        tmp_path,
        [unexpected_revision, target_revision],
    )

    result = run_bash(
        SMOKE_SCRIPT,
        "https://simulation.example.test",
        target_revision,
        old_revision,
        check=False,
        capture_output=True,
        env=smoke_test_env(tmp_path, state_file, revisions_file),
    )

    assert result.returncode == 1
    assert "Unexpected revision" in result.stderr
    assert state_file.read_text(encoding="utf-8") == "1"


def test_candidate_smoke_rejects_a_revision_mismatch_without_retrying(tmp_path: Path):
    old_revision = "policyengine-simulation-entry-00001-old"
    target_revision = "policyengine-simulation-entry-00002-new"
    state_file, revisions_file = write_fake_smoke_curl(
        tmp_path,
        [old_revision, target_revision],
    )

    result = run_bash(
        SMOKE_SCRIPT,
        "https://candidate.example.test",
        target_revision,
        check=False,
        capture_output=True,
        env=smoke_test_env(tmp_path, state_file, revisions_file),
    )

    assert result.returncode == 1
    assert "Expected revision" in result.stderr
    assert state_file.read_text(encoding="utf-8") == "1"


def test_stable_smoke_fails_when_the_previous_revision_does_not_converge(
    tmp_path: Path,
):
    old_revision = "policyengine-simulation-entry-00001-old"
    target_revision = "policyengine-simulation-entry-00002-new"
    state_file, revisions_file = write_fake_smoke_curl(
        tmp_path,
        [old_revision, old_revision, old_revision],
    )

    result = run_bash(
        SMOKE_SCRIPT,
        "https://simulation.example.test",
        target_revision,
        old_revision,
        check=False,
        capture_output=True,
        env=smoke_test_env(tmp_path, state_file, revisions_file),
    )

    assert result.returncode == 1
    assert "did not serve revision" in result.stderr
    assert state_file.read_text(encoding="utf-8") == "3"


def test_stage12_deployment_uses_the_api_owned_schema_directly():
    validation_script = STAGE12_VALIDATION_SCRIPT.read_text(encoding="utf-8")

    assert "STAGE12_DATABASE_URL_SECRET_NAME" in validation_script
    assert "policyengine_v2_runtime" in validation_script
    assert "stage12_infrastructure" in validation_script
    assert "STAGE12_PERSISTENCE_API_URL" not in validation_script
    assert "curl" not in validation_script


def test_stage12_runtime_uses_typed_dml_without_raw_sql_or_schema_changes() -> None:
    removed_adapter = (
        REPOSITORY_ROOT
        / "libs"
        / "policyengine-simulation-contract"
        / "src"
        / "policyengine_simulation_contract"
        / "stage12_persistence.py"
    )
    assert not removed_adapter.exists()

    removed_client = REPOSITORY_ROOT / "libs" / "policyengine-stage12-client"
    assert not removed_client.exists()
    persistence_source = (
        REPOSITORY_ROOT
        / "libs"
        / "policyengine-stage12-persistence"
        / "src"
        / "policyengine_stage12_persistence"
    )
    sources = "\n".join(
        path.read_text(encoding="utf-8") for path in persistence_source.glob("*.py")
    )
    assert "sqlalchemy" in sources
    assert "STAGE12_DATABASE_URL" not in sources
    assert "text(" not in sources
    assert "exec_driver_sql" not in sources
    assert ".cursor(" not in sources
    assert "create_all(" not in sources


def _assert_gcloud_deployment_jobs(
    reusable_workflow: str, cloud_run_deploy_script: str
) -> None:
    assert "deploy-cloud-run-simulation-entry.sh" in reusable_workflow
    assert "gcloud run deploy" in cloud_run_deploy_script
    assert "set-cloud-run-simulation-entry-revision.sh" in reusable_workflow
    assert "terraform" not in reusable_workflow.lower()
    assert "deploy_entrypoint:" in reusable_workflow
    assert "deploy_gateway:" in reusable_workflow
    assert "deploy_executor:" in reusable_workflow
    assert "update_routing:" in reusable_workflow
    assert "stage12_v2_ready:" in reusable_workflow
    assert "integration:" in reusable_workflow
    assert "authenticated_test:" in reusable_workflow
    assert "promote_entrypoint:" in reusable_workflow
    assert (
        "needs: [prepare, deploy_entrypoint, deploy_gateway, deploy_executor]"
        in reusable_workflow
    )


def _assert_beta_then_prod_release(
    deploy_workflow: str, reusable_workflow: str
) -> None:
    assert "needs: beta" in deploy_workflow
    assert deploy_workflow.index("beta:") < deploy_workflow.index("prod:")
    assert "release_environment: beta" in deploy_workflow
    assert "release_environment: prod" in deploy_workflow
    assert "deployment_environment: staging" in deploy_workflow
    assert "deployment_environment: production" in deploy_workflow
    assert deploy_workflow.count("deploy_existing_stack: true") == 2
    assert deploy_workflow.count("promote_entrypoint: true") == 2
    assert "promote_entrypoint: false" not in deploy_workflow
    assert "entrypoint_environment" not in deploy_workflow
    assert "entrypoint_environment" not in reusable_workflow
    assert "entrypoint_public_url" not in deploy_workflow
    assert "entrypoint_public_url" not in reusable_workflow
    assert "SIMULATION_ENTRYPOINT_PUBLIC_URL" not in reusable_workflow
    assert "staging.simulation.api.policyengine.org" not in deploy_workflow


def _assert_deployment_environment_settings(reusable_workflow: str) -> None:
    assert (
        # Sentinel, restore, final routing acknowledgement, the simulation
        # database migration and the staging economy check use secrets too.
        reusable_workflow.count("environment: ${{ inputs.release_environment }}") == 17
    )
    assert "APP_ENVIRONMENT: ${{ inputs.deployment_environment }}" in reusable_workflow
    assert "STAGE12_ENABLED_VALUE: ${{ vars.STAGE12_ENABLED }}" in reusable_workflow
    assert "STAGE12_COMPARISON_BACKEND_CONFIGURED" not in reusable_workflow
    assert "STAGE12_CONTROL_NAME" not in reusable_workflow
    assert "STAGE12_DISPATCH_MAX_IN_FLIGHT" not in reusable_workflow
    assert "STAGE12_DISPATCH_QUEUE_CAPACITY" not in reusable_workflow
    assert "STAGE12_DISPATCH_TIMEOUT_SECONDS" not in reusable_workflow
    assert (
        "STAGE12_ENVIRONMENT: ${{ inputs.deployment_environment }}" in reusable_workflow
    )
    assert "MODAL_ENVIRONMENT: ${{ inputs.modal_environment }}" in reusable_workflow
    assert "id-token: write" in reusable_workflow
    assert (
        reusable_workflow.count("vars.OLD_GATEWAY_AUTH_CLIENT_SECRET_SECRET_NAME") == 1
    )
    assert "simulation-entry-old-gateway-client-secret" not in reusable_workflow


def _assert_stage12_deploy_targets(stage12_workflow: str) -> None:
    assert "deploy-stage12-staging:" in stage12_workflow
    assert "deploy-stage12-production:" in stage12_workflow
    assert stage12_workflow.count("deploy_existing_stack: false") == 2
    assert stage12_workflow.count("deploy_stage12_v2: true") == 2
    assert "release_environment: beta" in stage12_workflow
    assert "deployment_environment: staging" in stage12_workflow
    assert "modal_environment: staging" in stage12_workflow
    assert "release_environment: prod" in stage12_workflow
    assert "deployment_environment: production" in stage12_workflow
    assert "modal_environment: main" in stage12_workflow
    assert "DEPLOY_STAGE12_PRODUCTION" in stage12_workflow


def test_deployment_uses_gcloud_workflow_without_terraform():
    deploy_workflow = DEPLOY_WORKFLOW.read_text(encoding="utf-8")
    reusable_workflow = REUSABLE_DEPLOY_WORKFLOW.read_text(encoding="utf-8")
    stage12_workflow = STAGE12_DEPLOY_WORKFLOW.read_text(encoding="utf-8")
    cloud_run_deploy_script = CLOUD_RUN_DEPLOY_SCRIPT.read_text(encoding="utf-8")

    _assert_gcloud_deployment_jobs(reusable_workflow, cloud_run_deploy_script)
    _assert_beta_then_prod_release(deploy_workflow, reusable_workflow)
    _assert_deployment_environment_settings(reusable_workflow)
    assert AUTHENTICATED_TESTS.joinpath("test_deployed_auth.py").is_file()
    assert not list(
        (REPOSITORY_ROOT / "projects" / "policyengine-simulation-entry" / "infra").glob(
            "*.tf"
        )
    )
    assert not (
        REPOSITORY_ROOT
        / ".github"
        / "scripts"
        / "configure-cloud-run-simulation-entry-domains.sh"
    ).exists()
    _assert_stage12_deploy_targets(stage12_workflow)


def _staging_deployment_job_ids(jobs: dict) -> list[str]:
    """Return the jobs that run the reusable deployment workflow for staging."""
    return [
        job_id
        for job_id, job in jobs.items()
        if job.get("uses") == REUSABLE_DEPLOY_WORKFLOW_REFERENCE
        and (job.get("with") or {}).get("deployment_environment") == "staging"
    ]


def staging_promotion_violations(jobs: dict) -> list[str]:
    """Return each staging deployment job that leaves stable traffic unchanged.

    Rule sim.deploy.staging-promotes-tested-revision: a staging deployment must
    pass ``promote_entrypoint: true`` to the reusable deployment workflow, as
    production deployments do. Any other value, including an expression,
    counts as a violation.
    """
    return [
        f"{job_id} must pass promote_entrypoint: true"
        for job_id in _staging_deployment_job_ids(jobs)
        if jobs[job_id]["with"].get("promote_entrypoint") is not True
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
    """
    bad_jobs = workflow_jobs(STAGING_PROMOTION_FIXTURES / "bad_simulation_deploy.yml")
    good_jobs = workflow_jobs(STAGING_PROMOTION_FIXTURES / "good_simulation_deploy.yml")

    assert staging_promotion_violations(bad_jobs) == [
        "beta must pass promote_entrypoint: true"
    ]
    assert staging_promotion_violations(good_jobs) == []
