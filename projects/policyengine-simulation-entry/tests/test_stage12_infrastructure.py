"""Static checks for bounded Stage 12 deployment validation."""

from __future__ import annotations

from pathlib import Path
import subprocess

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / ".github/scripts/stage12-validate-infrastructure.sh"
SYNC_SCRIPT = REPO_ROOT / ".github/scripts/stage12-sync-modal-secrets.sh"
DEPLOY_WORKFLOW = REPO_ROOT / ".github/workflows/simulation-deploy.reusable.yml"


def test_infrastructure_validation_is_bounded_and_valid_shell() -> None:
    result = subprocess.run(
        ["bash", "-n", str(SCRIPT)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    sync_result = subprocess.run(
        ["bash", "-n", str(SYNC_SCRIPT)],
        capture_output=True,
        text=True,
    )
    assert sync_result.returncode == 0, sync_result.stderr
    source = SCRIPT.read_text(encoding="utf-8")
    assert "STAGE12_ENVIRONMENT" in source
    assert "STAGE12_ARTIFACT_BUCKET" in source
    assert "Pre-provisioned Stage 12 database, secret, and storage access" in source
    assert "STAGE12_GCP_CREDENTIALS_SECRET_NAME" in source
    assert "gcloud secrets versions list" in source
    assert "gcloud secrets versions access" in source
    assert "gcloud secrets create" not in source
    assert "gcloud secrets versions add" not in source
    assert "gcloud iam service-accounts create" not in source
    assert "gcloud iam service-accounts keys create" not in source
    assert "gcloud storage buckets create" not in source
    assert "gcloud storage buckets update" not in source
    assert "add-iam-policy-binding" not in source
    assert "gcloud storage cp" in source
    assert "gcloud storage rm" in source
    assert "stage-12-evaluation/_deployment-validation" in source
    assert "stage12_infrastructure" in source
    assert "get-iam-policy" in source
    assert "STAGE12_DATABASE_ADMIN_URL" not in source
    assert "STAGE12_DATABASE_ROLE" not in source
    assert "psql" not in source
    assert "CREATE TABLE" not in source
    assert "ALTER TABLE" not in source

    modal_sync = SYNC_SCRIPT.read_text(encoding="utf-8")
    assert "--from-json" in modal_sync
    assert "stage12-evaluation-runtime" in modal_sync
    assert "stage12-evaluation-gcp-credentials" in modal_sync
    assert "STAGE12_DATABASE_URL=" not in modal_sync
    assert "GOOGLE_APPLICATION_CREDENTIALS_JSON=" not in modal_sync


def test_infrastructure_validation_rejects_missing_configuration() -> None:
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        capture_output=True,
        text=True,
        env={},
    )
    assert result.returncode != 0
    assert "STAGE12_ENVIRONMENT is required" in result.stderr


def test_modal_secret_sync_rejects_missing_configuration() -> None:
    result = subprocess.run(
        ["bash", str(SYNC_SCRIPT), "staging"],
        capture_output=True,
        text=True,
        env={},
    )
    assert result.returncode != 0
    assert "STAGE12_GCP_PROJECT_ID is required" in result.stderr


def test_infrastructure_validation_installs_its_locked_dependencies() -> None:
    workflow = DEPLOY_WORKFLOW.read_text(encoding="utf-8")
    start = workflow.index("  configure_stage12_v2:")
    section = workflow[start : workflow.index("\n  deploy_stage12_v2:", start)]

    assert "actions/setup-python@v6" in section
    assert "astral-sh/setup-uv@v8.1.0" in section
    assert "uv sync --frozen" in section
    assert section.index("uv sync --frozen") < section.index(
        "stage12-validate-infrastructure.sh"
    )
