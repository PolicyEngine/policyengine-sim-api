from __future__ import annotations

import os
import subprocess
import tomllib
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
DOMAIN_SCRIPT = (
    REPOSITORY_ROOT
    / ".github"
    / "scripts"
    / "configure-cloud-run-simulation-entry-domains.sh"
)
TRAFFIC_SCRIPT = (
    REPOSITORY_ROOT
    / ".github"
    / "scripts"
    / "set-cloud-run-simulation-entry-revision.sh"
)
SMOKE_SCRIPT = (
    REPOSITORY_ROOT
    / ".github"
    / "scripts"
    / "cloud-run-simulation-entry-smoke.sh"
)
DEPLOY_WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "simulation-deploy.yml"
REUSABLE_DEPLOY_WORKFLOW = (
    REPOSITORY_ROOT / ".github" / "workflows" / "simulation-deploy.reusable.yml"
)
PUBLISH_WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "publish-clients.yml"
AUTHENTICATED_TESTS = (
    REPOSITORY_ROOT
    / "projects"
    / "policyengine-simulation-entry"
    / "authenticated_tests"
)
DOCKERIGNORE = (
    REPOSITORY_ROOT
    / "projects"
    / "policyengine-simulation-entry"
    / "Dockerfile.dockerignore"
)


def test_operator_scripts_have_valid_shell_syntax():
    subprocess.run(["bash", "-n", DOMAIN_SCRIPT], check=True)
    subprocess.run(["bash", "-n", TRAFFIC_SCRIPT], check=True)
    subprocess.run(["bash", "-n", SMOKE_SCRIPT], check=True)
    assert os.access(SMOKE_SCRIPT, os.X_OK)


def test_deployment_uses_gcloud_workflow_without_terraform():
    deploy_workflow = DEPLOY_WORKFLOW.read_text(encoding="utf-8")
    reusable_workflow = REUSABLE_DEPLOY_WORKFLOW.read_text(encoding="utf-8")

    assert "gcloud run deploy" in reusable_workflow
    assert "update-traffic" not in reusable_workflow
    assert "set-cloud-run-simulation-entry-revision.sh" not in reusable_workflow
    assert "terraform" not in reusable_workflow.lower()
    assert "deploy_entrypoint:" in reusable_workflow
    assert "deploy_gateway:" in reusable_workflow
    assert "deploy_executor:" in reusable_workflow
    assert "update_routing:" in reusable_workflow
    assert "integration:" in reusable_workflow
    assert "authenticated_test:" in reusable_workflow
    assert (
        "needs: [prepare, deploy_entrypoint, deploy_gateway, deploy_executor]"
        in reusable_workflow
    )
    assert "needs: beta" in deploy_workflow
    assert deploy_workflow.index("beta:") < deploy_workflow.index("prod:")
    assert "release_environment: beta" in deploy_workflow
    assert "release_environment: prod" in deploy_workflow
    assert "entrypoint_environment" not in deploy_workflow
    assert "entrypoint_environment" not in reusable_workflow
    assert (
        reusable_workflow.count(
            "environment: ${{ inputs.release_environment }}"
        )
        == 7
    )
    assert "APP_ENVIRONMENT=${{ inputs.release_environment }}" in reusable_workflow
    assert "id-token: write" in reusable_workflow
    assert (
        reusable_workflow.count("vars.OLD_GATEWAY_AUTH_CLIENT_SECRET_SECRET_NAME") == 1
    )
    assert "simulation-entry-old-gateway-client-secret" not in reusable_workflow
    assert AUTHENTICATED_TESTS.joinpath("test_deployed_auth.py").is_file()
    assert not list(
        (REPOSITORY_ROOT / "projects" / "policyengine-simulation-entry" / "infra").glob(
            "*.tf"
        )
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
    assert reusable_workflow.index("update_routing:") < reusable_workflow.index(
        "integration:"
    )
    assert reusable_workflow.index("integration:") < reusable_workflow.index(
        "authenticated_test:"
    )
    assert "needs: beta" in deploy_workflow
    assert "skip_beta" not in deploy_workflow


def test_traffic_changes_are_operator_run_and_revision_validated():
    script = TRAFFIC_SCRIPT.read_text(encoding="utf-8")

    assert "run revisions describe" in script
    assert 'select(.type == "Ready" and .status == "True")' in script
    assert '["serving.knative.dev/service"]' in script
    assert 'run services update-traffic "${service}"' in script
    assert '--to-revisions "${revision}=100"' in script

    result = subprocess.run(
        ["bash", TRAFFIC_SCRIPT],
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "SIMULATION_ENTRYPOINT_GCP_PROJECT_ID": "simulation-entry-test",
            "SIMULATION_ENTRYPOINT_DEPLOYMENT_ENVIRONMENT": "production",
            "SIMULATION_ENTRYPOINT_TARGET_REVISION": "policyengine-simulation-entry-00001-abc",
            "SIMULATION_ENTRYPOINT_TRAFFIC_DRY_RUN": "1",
        },
    )

    assert "policyengine-simulation-entry-00001-abc=100" in result.stdout
    assert "policyengine-simulation-entry-staging" not in result.stdout


def test_container_context_excludes_local_environments_and_unrelated_projects():
    ignore_rules = DOCKERIGNORE.read_text(encoding="utf-8")

    assert "**/.venv" in ignore_rules
    assert "projects/*" in ignore_rules
    assert "!projects/policyengine-simulation-entry/**" in ignore_rules
    assert "projects/policyengine-simulation-entry/authenticated_tests" in ignore_rules
    assert "libs/*" in ignore_rules
    assert ignore_rules.index("**/.venv") > ignore_rules.index(
        "!projects/policyengine-simulation-entry/**"
    )
    assert ignore_rules.index("**/.venv") > ignore_rules.index(
        "!libs/policyengine-simulation-contract/**"
    )


def test_production_lock_excludes_modal_and_database_runtime_packages():
    lockfile = tomllib.loads(
        (
            REPOSITORY_ROOT / "projects" / "policyengine-simulation-entry" / "uv.lock"
        ).read_text(encoding="utf-8")
    )
    resolved_packages = {package["name"] for package in lockfile["package"]}

    for package in ("modal", "policyengine-fastapi", "sqlalchemy", "sqlmodel"):
        assert package not in resolved_packages


def test_domain_mapping_dry_run_targets_both_services():
    result = subprocess.run(
        ["bash", DOMAIN_SCRIPT],
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "SIMULATION_ENTRYPOINT_GCP_PROJECT_ID": "simulation-entry-test",
            "SIMULATION_ENTRYPOINT_DOMAINS_DRY_RUN": "1",
        },
    )

    assert "policyengine-simulation-entry-staging" in result.stdout
    assert "staging.simulation.api.policyengine.org" in result.stdout
    assert "policyengine-simulation-entry" in result.stdout
    assert "simulation.api.policyengine.org" in result.stdout


def test_client_publication_checks_out_the_deployed_commit():
    workflow = PUBLISH_WORKFLOW.read_text(encoding="utf-8")

    assert "github.event.workflow_run.head_sha" in workflow
