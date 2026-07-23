from __future__ import annotations

import os
import subprocess
import tomllib
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
BOOTSTRAP_SCRIPT = (
    REPOSITORY_ROOT / ".github" / "scripts" / "bootstrap-cloud-run-simulation-api.sh"
)
DOMAIN_SCRIPT = (
    REPOSITORY_ROOT
    / ".github"
    / "scripts"
    / "configure-cloud-run-simulation-api-domains.sh"
)
TRAFFIC_SCRIPT = (
    REPOSITORY_ROOT / ".github" / "scripts" / "set-cloud-run-simulation-api-revision.sh"
)
WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "cloud-run-simulation-api.yml"
DOCKERIGNORE = (
    REPOSITORY_ROOT
    / "projects"
    / "policyengine-simulation-api"
    / "Dockerfile.dockerignore"
)


def test_bootstrap_script_has_valid_shell_syntax():
    subprocess.run(["bash", "-n", BOOTSTRAP_SCRIPT], check=True)
    subprocess.run(["bash", "-n", DOMAIN_SCRIPT], check=True)
    subprocess.run(["bash", "-n", TRAFFIC_SCRIPT], check=True)


def test_bootstrap_dry_run_is_self_contained():
    environment = {
        **os.environ,
        "SIMULATION_GCP_PROJECT_ID": "simulation-api-test",
        "SIMULATION_BOOTSTRAP_DRY_RUN": "1",
    }

    result = subprocess.run(
        ["bash", BOOTSTRAP_SCRIPT],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert "projects create simulation-api-test" in result.stdout
    assert "artifacts repositories create policyengine-simulation-api" in result.stdout
    assert "roles/serviceusage.serviceUsageConsumer" in result.stdout
    assert "Bootstrap complete" in result.stdout
    assert "000000000000" in result.stdout


def test_deployment_uses_gcloud_workflow_without_terraform():
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "gcloud run deploy" in workflow
    assert "update-traffic" not in workflow
    assert "set-cloud-run-simulation-api-revision.sh" not in workflow
    assert "terraform" not in workflow.lower()
    assert not list(
        (REPOSITORY_ROOT / "projects" / "policyengine-simulation-api" / "infra").glob(
            "*.tf"
        )
    )


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
            "SIMULATION_GCP_PROJECT_ID": "simulation-api-test",
            "SIMULATION_DEPLOYMENT_ENVIRONMENT": "production",
            "SIMULATION_TARGET_REVISION": "policyengine-simulation-api-00001-abc",
            "SIMULATION_TRAFFIC_DRY_RUN": "1",
        },
    )

    assert "policyengine-simulation-api-00001-abc=100" in result.stdout
    assert "policyengine-simulation-api-staging" not in result.stdout


def test_container_context_excludes_local_environments_and_unrelated_projects():
    ignore_rules = DOCKERIGNORE.read_text(encoding="utf-8")

    assert "**/.venv" in ignore_rules
    assert "projects/*" in ignore_rules
    assert "!projects/policyengine-simulation-api/**" in ignore_rules
    assert "libs/*" in ignore_rules


def test_production_lock_excludes_modal_and_database_runtime_packages():
    lockfile = tomllib.loads(
        (
            REPOSITORY_ROOT / "projects" / "policyengine-simulation-api" / "uv.lock"
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
            "SIMULATION_GCP_PROJECT_ID": "simulation-api-test",
            "SIMULATION_DOMAINS_DRY_RUN": "1",
        },
    )

    assert "policyengine-simulation-api-staging" in result.stdout
    assert "staging.simulation.api.policyengine.org" in result.stdout
    assert "policyengine-simulation-api" in result.stdout
    assert "simulation.api.policyengine.org" in result.stdout
