"""Tests for the Cloud Run deployment's environment, permissions and revision tags."""

from __future__ import annotations

import json
import os
import re
import subprocess
import textwrap
from pathlib import Path

import pytest
from test_deployment_assets_support import (
    CLOUD_RUN_DEPLOY_SCRIPT,
    DEPLOY_WORKFLOW,
    DOCKERFILE,
    REUSABLE_DEPLOY_WORKFLOW,
    STAGE12_DEPLOY_WORKFLOW,
)


def test_cloud_run_deployment_does_not_interpolate_github_values_in_shell():
    reusable_workflow = REUSABLE_DEPLOY_WORKFLOW.read_text(encoding="utf-8")
    deployment_script = CLOUD_RUN_DEPLOY_SCRIPT.read_text(encoding="utf-8")
    step_start = reusable_workflow.index(
        "      - name: Deploy Cloud Run entrypoint candidate"
    )
    step_end = reusable_workflow.index(
        "\n      - name: Resolve exact candidate metadata", step_start
    )
    deployment_step = reusable_workflow[step_start:step_end]
    run_block = deployment_step[deployment_step.index("\n        run:") :]

    assert "${{" not in run_block
    assert ".github/scripts/deploy-cloud-run-simulation-entry.sh" in run_block
    assert "--env-vars-file" in deployment_script
    assert "--set-env-vars" not in deployment_script
    assert "gcloud secrets versions list" in deployment_script
    assert ":latest" not in deployment_script


def cloud_run_deploy_environment(tmp_path: Path) -> dict[str, str]:
    """Return a complete deployment environment backed by a fake gcloud."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_gcloud = fake_bin / "gcloud"
    fake_gcloud.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            set -euo pipefail

            if [[ "$1 $2 $3" == "secrets versions list" ]]; then
              printf '%s\n' 1 3 2
              exit 0
            fi

            if [[ "$1 $2" == "run deploy" ]]; then
              printf '%s\n' "$@" > "${GCLOUD_ARGUMENTS_CAPTURE}"
              while (($#)); do
                if [[ "$1" == "--env-vars-file" ]]; then
                  cp "$2" "${GCLOUD_ENV_FILE_CAPTURE}"
                  exit 0
                fi
                shift
              done
            fi

            printf 'unexpected gcloud invocation\n' >&2
            exit 1
            """
        ),
        encoding="utf-8",
    )
    fake_gcloud.chmod(0o755)
    environment = {
        name: value
        for name, value in os.environ.items()
        if name != "SIMULATION_ENTRYPOINT_ENFORCE_PERMISSIONS_VALUE"
    }
    return environment | {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "RUNNER_TEMP": str(tmp_path),
        "GCLOUD_ARGUMENTS_CAPTURE": str(tmp_path / "gcloud-arguments.txt"),
        "GCLOUD_ENV_FILE_CAPTURE": str(tmp_path / "runtime-environment.json"),
        "PROJECT_ID": "policyengine-staging",
        "REGION": "us-central1",
        "IMAGE": "us-central1-docker.pkg.dev/project/repository/image:sha",
        "TAG": "s-123",
        "DEPLOY_STAGE12_V2": "true",
        "APP_ENVIRONMENT": "staging",
        "MODAL_ENVIRONMENT": "staging",
        "ENTRYPOINT_SERVICE": "policyengine-simulation-entry-staging",
        "ENTRYPOINT_RUNTIME_SERVICE_ACCOUNT": (
            "simulation-entry@policyengine-staging.iam.gserviceaccount.com"
        ),
        "ENTRYPOINT_MIN_INSTANCES": "0",
        "ENTRYPOINT_MAX_INSTANCES": "10",
        "SIMULATION_ENTRYPOINT_AUTH_ISSUER_VALUE": (
            "https://issuer.example/path?literal=$(not-a-command)"
        ),
        "SIMULATION_ENTRYPOINT_AUTH_AUDIENCE_VALUE": "simulation-api",
        "OLD_GATEWAY_URL_VALUE": "https://legacy.example",
        "OLD_GATEWAY_AUTH_ISSUER_VALUE": "https://issuer.example",
        "OLD_GATEWAY_AUTH_AUDIENCE_VALUE": "legacy-api",
        "OLD_GATEWAY_AUTH_CLIENT_ID_VALUE": "entrypoint-client",
        "OLD_GATEWAY_AUTH_CLIENT_SECRET_SECRET_NAME": "old-client-secret",
        "OBSERVABILITY_SERVICE_NAMESPACE": "policyengine.simulation-entry",
        "OBSERVABILITY_TRACE_PROJECT_ID": "policyengine-observability",
        "OTEL_EXPORTER_OTLP_ENDPOINT": "https://collector.example",
        "POLICYENGINE_OTEL_GOOGLE_AUDIENCE": "https://collector.example",
        "STAGE12_ENABLED_VALUE": "0",
        "STAGE12_ARTIFACT_BUCKET_VALUE": "policyengine-stage12-staging",
        "STAGE12_DATABASE_URL_SECRET_NAME": "stage12-staging-database-url",
        "MODAL_TOKEN_ID_SECRET_NAME": "modal-token-id",
        "MODAL_TOKEN_SECRET_SECRET_NAME": "modal-token-secret",
        "SIMULATION_DATABASE_URL_SECRET_NAME": (
            "simulation-staging-runtime-database-url"
        ),
    }


def test_cloud_run_deployment_escapes_environment_and_pins_secret_versions(tmp_path):
    command_environment = cloud_run_deploy_environment(tmp_path)
    arguments_capture = Path(command_environment["GCLOUD_ARGUMENTS_CAPTURE"])
    env_capture = Path(command_environment["GCLOUD_ENV_FILE_CAPTURE"])

    subprocess.run(
        [CLOUD_RUN_DEPLOY_SCRIPT],
        check=True,
        env=command_environment,
    )

    runtime_environment = json.loads(env_capture.read_text(encoding="utf-8"))
    assert runtime_environment["SIMULATION_ENTRYPOINT_AUTH_ISSUER"] == (
        "https://issuer.example/path?literal=$(not-a-command)"
    )
    assert runtime_environment["STAGE12_ENABLED"] == "0"
    assert runtime_environment["STAGE12_V2_MANIFEST_ENVIRONMENT"] == "staging"
    assert runtime_environment["OBSERVABILITY_SERVICE_NAMESPACE"] == (
        "policyengine.simulation-entry"
    )
    assert runtime_environment["OBSERVABILITY_TRACE_PROJECT_ID"] == (
        "policyengine-observability"
    )
    assert runtime_environment["CLOUD_RUN_REGION"] == "us-central1"
    assert runtime_environment["OTEL_EXPORTER_OTLP_ENDPOINT"] == (
        "https://collector.example"
    )
    assert runtime_environment["POLICYENGINE_OTEL_GOOGLE_AUDIENCE"] == (
        "https://collector.example"
    )
    assert "STAGE12_DATABASE_URL" not in runtime_environment
    arguments = arguments_capture.read_text(encoding="utf-8").splitlines()
    secrets_argument = arguments[arguments.index("--set-secrets") + 1]
    assert secrets_argument == (
        "OLD_GATEWAY_AUTH_CLIENT_SECRET=old-client-secret:3,"
        "SIMULATION_DATABASE_URL=simulation-staging-runtime-database-url:3,"
        "MODAL_TOKEN_ID=modal-token-id:3,"
        "MODAL_TOKEN_SECRET=modal-token-secret:3,"
        "STAGE12_DATABASE_URL=stage12-staging-database-url:3"
    )
    assert ":latest" not in secrets_argument


@pytest.mark.parametrize(
    ("configured", "expected"),
    [(None, "0"), ("", "0"), ("0", "0"), ("1", "1")],
    ids=["unset", "empty", "log-only", "enforced"],
)
def test_cloud_run_deployment_defaults_permission_enforcement_to_log_only(
    tmp_path,
    configured,
    expected,
):
    command_environment = cloud_run_deploy_environment(tmp_path)
    if configured is not None:
        command_environment["SIMULATION_ENTRYPOINT_ENFORCE_PERMISSIONS_VALUE"] = (
            configured
        )

    subprocess.run(
        [CLOUD_RUN_DEPLOY_SCRIPT],
        check=True,
        env=command_environment,
    )

    runtime_environment = json.loads(
        Path(command_environment["GCLOUD_ENV_FILE_CAPTURE"]).read_text(encoding="utf-8")
    )
    assert runtime_environment["SIMULATION_ENTRYPOINT_ENFORCE_PERMISSIONS"] == expected


@pytest.mark.parametrize("configured", ["true", "yes", "2", " 1"])
def test_cloud_run_deployment_rejects_invalid_permission_enforcement(
    tmp_path,
    configured,
):
    command_environment = cloud_run_deploy_environment(tmp_path)
    command_environment["SIMULATION_ENTRYPOINT_ENFORCE_PERMISSIONS_VALUE"] = configured

    result = subprocess.run(
        [CLOUD_RUN_DEPLOY_SCRIPT],
        check=False,
        env=command_environment,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert (
        "SIMULATION_ENTRYPOINT_ENFORCE_PERMISSIONS_VALUE must be 0 or 1"
        in result.stderr
    )
    assert not Path(command_environment["GCLOUD_ENV_FILE_CAPTURE"]).exists()


def test_permission_enforcement_reaches_candidate_and_authenticated_tests():
    reusable_workflow = REUSABLE_DEPLOY_WORKFLOW.read_text(encoding="utf-8")

    assert (
        reusable_workflow.count(
            "SIMULATION_ENTRYPOINT_ENFORCE_PERMISSIONS_VALUE: "
            "${{ vars.SIMULATION_ENTRYPOINT_ENFORCE_PERMISSIONS }}"
        )
        == 1
    )
    # Every deployed authentication run must know whether the target revision
    # enforces permissions, so its enforcement check runs or skips correctly.
    authenticated_runs = reusable_workflow.count(
        "run: uv run --frozen pytest authenticated_tests/ -v"
    )
    assert authenticated_runs == 3
    assert (
        reusable_workflow.count(
            "SIMULATION_ENTRYPOINT_TEST_ENFORCE_PERMISSIONS: "
            "${{ vars.SIMULATION_ENTRYPOINT_ENFORCE_PERMISSIONS }}"
        )
        == authenticated_runs
    )


def test_cloud_run_revision_tags_are_valid_on_the_first_workflow_run():
    deploy_workflow = DEPLOY_WORKFLOW.read_text(encoding="utf-8")
    reusable_workflow = REUSABLE_DEPLOY_WORKFLOW.read_text(encoding="utf-8")
    prefixes = re.findall(
        r"^\s+entrypoint_revision_prefix:\s+(\S+)$",
        deploy_workflow,
        flags=re.MULTILINE,
    )

    assert (
        "tag=${{ inputs.entrypoint_revision_prefix }}${GITHUB_RUN_NUMBER}"
        in reusable_workflow
    )
    assert len(prefixes) == 2

    first_run_tags = [f"{prefix}1" for prefix in prefixes]
    for tag in first_run_tags:
        assert 3 <= len(tag) <= 63
        assert re.fullmatch(r"[a-z][a-z0-9-]*[a-z0-9]", tag)


def test_revision_tags_fit_existing_cloud_run_service_names():
    deploy_workflow = DEPLOY_WORKFLOW.read_text(encoding="utf-8")
    stage12_workflow = STAGE12_DEPLOY_WORKFLOW.read_text(encoding="utf-8")
    deployments = {
        "policyengine-simulation-entry-staging": "s-",
        "policyengine-simulation-entry": "p-",
    }

    for workflow in (deploy_workflow, stage12_workflow):
        for service, prefix in deployments.items():
            assert f"entrypoint_service: {service}" in workflow
            assert f"entrypoint_revision_prefix: {prefix}" in workflow
            # Reserve seven digits for the monotonically increasing workflow
            # run number while satisfying Cloud Run's service-plus-tag limit.
            assert len(service) + len(f"{prefix}9999999") <= 46


def test_entrypoint_container_uses_requested_target_platform():
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")

    assert "$BUILDPLATFORM" not in dockerfile
