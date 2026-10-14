from __future__ import annotations

import json
import os
import re
import subprocess
import textwrap
from pathlib import Path

import pytest
import tomllib

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
GITIGNORE = REPOSITORY_ROOT / ".gitignore"
TRAFFIC_SCRIPT = (
    REPOSITORY_ROOT
    / ".github"
    / "scripts"
    / "set-cloud-run-simulation-entry-revision.sh"
)
SMOKE_SCRIPT = (
    REPOSITORY_ROOT / ".github" / "scripts" / "cloud-run-simulation-entry-smoke.sh"
)
STAGE12_VALIDATION_SCRIPT = (
    REPOSITORY_ROOT / ".github" / "scripts" / "stage12-validate-infrastructure.sh"
)
CLOUD_RUN_DEPLOY_SCRIPT = (
    REPOSITORY_ROOT / ".github" / "scripts" / "deploy-cloud-run-simulation-entry.sh"
)
DEPLOYMENT_READY_SCRIPT = (
    REPOSITORY_ROOT / ".github" / "scripts" / "simulation-deployment-ready.sh"
)
DEPLOY_WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "simulation-deploy.yml"
REUSABLE_DEPLOY_WORKFLOW = (
    REPOSITORY_ROOT / ".github" / "workflows" / "simulation-deploy.reusable.yml"
)
ROLLBACK_WORKFLOW = (
    REPOSITORY_ROOT / ".github" / "workflows" / "simulation-rollback.yml"
)
STAGE12_DEPLOY_WORKFLOW = (
    REPOSITORY_ROOT / ".github" / "workflows" / "stage12-deploy.yml"
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
DOCKERFILE = (
    REPOSITORY_ROOT / "projects" / "policyengine-simulation-entry" / "Dockerfile"
)


def _run_bash(
    *arguments: str | Path,
    check: bool,
    capture_output: bool = False,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run bash on a repository deployment script, with text output."""
    return subprocess.run(
        ["bash", *arguments],
        check=check,
        capture_output=capture_output,
        text=True,
        env=env,
        cwd=cwd,
    )


def test_deployment_scripts_have_valid_shell_syntax():
    _run_bash("-n", TRAFFIC_SCRIPT, check=True)
    _run_bash("-n", SMOKE_SCRIPT, check=True)
    _run_bash("-n", CLOUD_RUN_DEPLOY_SCRIPT, check=True)
    assert os.access(SMOKE_SCRIPT, os.X_OK)
    assert os.access(CLOUD_RUN_DEPLOY_SCRIPT, os.X_OK)


def _write_fake_smoke_curl(tmp_path: Path, revisions: list[str]) -> tuple[Path, Path]:
    state_file = tmp_path / "health-request-count"
    revisions_file = tmp_path / "health-revisions"
    revisions_file.write_text("\n".join(revisions) + "\n", encoding="utf-8")
    fake_curl = tmp_path / "curl"
    fake_curl.write_text(
        textwrap.dedent("""\
            #!/usr/bin/env bash
            set -euo pipefail

            headers_file=""
            body_file=""
            url=""
            while [ "$#" -gt 0 ]; do
              case "$1" in
                --dump-header|--output|--request|--header|--data)
                  option="$1"
                  value="$2"
                  shift 2
                  case "${option}" in
                    --dump-header) headers_file="${value}" ;;
                    --output) body_file="${value}" ;;
                  esac
                  ;;
                --fail|--silent|--show-error)
                  shift
                  ;;
                *)
                  url="$1"
                  shift
                  ;;
              esac
            done

            case "${url}" in
              */health)
                count=0
                if [ -f "${FAKE_CURL_STATE}" ]; then
                  count="$(cat "${FAKE_CURL_STATE}")"
                fi
                line_number=$((count + 1))
                revision="$(sed -n "${line_number}p" "${FAKE_CURL_REVISIONS}")"
                printf '%s' "${line_number}" > "${FAKE_CURL_STATE}"
                printf 'HTTP/1.1 200 OK\r\nX-PolicyEngine-Simulation-Revision: %s\r\n\r\n' \
                  "${revision}" > "${headers_file}"
                printf '{"status":"healthy"}' > "${body_file}"
                ;;
              */ready)
                printf '{"status":"ready"}'
                ;;
              */versions)
                versions='{}'
                if [ -n "${FAKE_CURL_VERSIONS:-}" ]; then
                  versions="$(cat "${FAKE_CURL_VERSIONS}")"
                fi
                if [ -n "${body_file}" ]; then
                  printf '%s' "${versions}" > "${body_file}"
                else
                  printf '%s' "${versions}"
                fi
                ;;
              */ping)
                printf '{"incremented":2}'
                ;;
              *)
                printf 'Unexpected fake curl URL: %s\n' "${url}" >&2
                exit 2
                ;;
            esac
            """),  # noqa: E501  # the fake curl writes its HTTP header block with one printf
        encoding="utf-8",
    )
    fake_curl.chmod(0o755)
    return state_file, revisions_file


def _smoke_test_env(
    tmp_path: Path,
    state_file: Path,
    revisions_file: Path,
) -> dict[str, str]:
    return {
        **os.environ,
        "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
        "FAKE_CURL_STATE": str(state_file),
        "FAKE_CURL_REVISIONS": str(revisions_file),
        "SIMULATION_ENTRYPOINT_ROUTING_MAX_ATTEMPTS": "3",
        "SIMULATION_ENTRYPOINT_ROUTING_RETRY_DELAY_SECONDS": "0",
    }


def test_stable_smoke_retries_the_previous_revision_until_target_is_served(
    tmp_path: Path,
):
    old_revision = "policyengine-simulation-entry-00001-old"
    target_revision = "policyengine-simulation-entry-00002-new"
    state_file, revisions_file = _write_fake_smoke_curl(
        tmp_path,
        [old_revision, old_revision, target_revision],
    )

    _run_bash(
        SMOKE_SCRIPT,
        "https://simulation.example.test",
        target_revision,
        old_revision,
        check=True,
        capture_output=True,
        env=_smoke_test_env(tmp_path, state_file, revisions_file),
    )

    assert state_file.read_text(encoding="utf-8") == "3"


def test_stable_smoke_rejects_an_unexpected_revision_without_retrying(tmp_path: Path):
    old_revision = "policyengine-simulation-entry-00001-old"
    target_revision = "policyengine-simulation-entry-00002-new"
    unexpected_revision = "policyengine-simulation-entry-00003-unexpected"
    state_file, revisions_file = _write_fake_smoke_curl(
        tmp_path,
        [unexpected_revision, target_revision],
    )

    result = _run_bash(
        SMOKE_SCRIPT,
        "https://simulation.example.test",
        target_revision,
        old_revision,
        check=False,
        capture_output=True,
        env=_smoke_test_env(tmp_path, state_file, revisions_file),
    )

    assert result.returncode == 1
    assert "Unexpected revision" in result.stderr
    assert state_file.read_text(encoding="utf-8") == "1"


def test_candidate_smoke_rejects_a_revision_mismatch_without_retrying(tmp_path: Path):
    old_revision = "policyengine-simulation-entry-00001-old"
    target_revision = "policyengine-simulation-entry-00002-new"
    state_file, revisions_file = _write_fake_smoke_curl(
        tmp_path,
        [old_revision, target_revision],
    )

    result = _run_bash(
        SMOKE_SCRIPT,
        "https://candidate.example.test",
        target_revision,
        check=False,
        capture_output=True,
        env=_smoke_test_env(tmp_path, state_file, revisions_file),
    )

    assert result.returncode == 1
    assert "Expected revision" in result.stderr
    assert state_file.read_text(encoding="utf-8") == "1"


def test_stable_smoke_fails_when_the_previous_revision_does_not_converge(
    tmp_path: Path,
):
    old_revision = "policyengine-simulation-entry-00001-old"
    target_revision = "policyengine-simulation-entry-00002-new"
    state_file, revisions_file = _write_fake_smoke_curl(
        tmp_path,
        [old_revision, old_revision, old_revision],
    )

    result = _run_bash(
        SMOKE_SCRIPT,
        "https://simulation.example.test",
        target_revision,
        old_revision,
        check=False,
        capture_output=True,
        env=_smoke_test_env(tmp_path, state_file, revisions_file),
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
    assert "promote_entrypoint: false" in deploy_workflow
    assert "promote_entrypoint: true" in deploy_workflow
    assert "entrypoint_environment" not in deploy_workflow
    assert "entrypoint_environment" not in reusable_workflow
    assert "entrypoint_public_url" not in deploy_workflow
    assert "entrypoint_public_url" not in reusable_workflow
    assert "SIMULATION_ENTRYPOINT_PUBLIC_URL" not in reusable_workflow
    assert "staging.simulation.api.policyengine.org" not in deploy_workflow


def _assert_deployment_environment_settings(reusable_workflow: str) -> None:
    assert (
        # Sentinel, restore and final routing acknowledgement use secrets too.
        reusable_workflow.count("environment: ${{ inputs.release_environment }}") == 15
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


def _cloud_run_deploy_environment(tmp_path: Path) -> dict[str, str]:
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
    }


def test_cloud_run_deployment_escapes_environment_and_pins_secret_versions(tmp_path):
    command_environment = _cloud_run_deploy_environment(tmp_path)
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
    command_environment = _cloud_run_deploy_environment(tmp_path)
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
    command_environment = _cloud_run_deploy_environment(tmp_path)
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
        "run: uv run pytest authenticated_tests/ -v"
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
        _workflow_jobs(REUSABLE_DEPLOY_WORKFLOW)["promote_entrypoint"]["needs"]
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
    finalizer_job = _job_section(reusable_workflow, "finalize_routing")
    assert "inputs.deploy_existing_stack &&" in finalizer_job
    sentinel_job = _job_section(reusable_workflow, "sentinel_expected")
    assert "if: ${{ inputs.deploy_existing_stack }}" in sentinel_job


def _job_section(workflow: str, job_name: str) -> str:
    match = re.search(
        rf"^  {job_name}:\n(?P<body>.*?)(?=^  [a-z0-9_-]+:\n|\Z)",
        workflow,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert match is not None, job_name
    return match.group("body")


def _sentinel_check_step(job: dict) -> dict:
    steps = [
        step
        for step in job["steps"]
        if "src.modal.utils.release_sentinel check" in step.get("run", "")
    ]
    assert len(steps) == 1
    return steps[0]


def test_deployed_stack_must_reproduce_the_numerical_sentinel():
    workflow = REUSABLE_DEPLOY_WORKFLOW.read_text(encoding="utf-8")
    jobs = _workflow_jobs(REUSABLE_DEPLOY_WORKFLOW)
    sentinel_job = _job_section(workflow, "sentinel_expected")
    assert jobs["sentinel_expected"]["needs"] == "prepare"
    assert "release_sentinel expected" in sentinel_job
    assert "secrets.PE_UK_PRIVATE_HF_READ_TOKEN" in sentinel_job
    assert "secrets.GCP_CREDENTIALS_JSON" in sentinel_job
    assert ".uk.dataset_sha256 == $d" in sentinel_job

    integration = jobs["integration"]
    assert "needs.sentinel_expected.result == 'success'" in integration["if"]
    assert "needs.sentinel_expected.result == 'skipped'" in integration["if"]
    check = _sentinel_check_step(integration)
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
    stable = _sentinel_check_step(promote)
    assert stable["if"] == "${{ inputs.deploy_existing_stack }}"
    assert '--base-url "${STABLE_URL}"' in stable["run"]
    names = [step.get("name", "") for step in promote["steps"]]
    # A failing stable sentinel triggers the existing traffic restore step.
    assert promote["steps"].index(stable) < next(
        index
        for index, name in enumerate(names)
        if name.startswith("Restore previous revision")
    )


# A small evaluator for the GitHub Actions expression subset these workflows
# use (&&, ||, !, ==, !=, parentheses, status functions, property access and
# string literals), so conditions are tested by what they decide, not by
# their text. It follows GitHub's rules where they matter here: a job ``if``
# with no status function is implicitly ``success() && (...)``; ``==``
# compares strings case-insensitively and converts mismatched types to
# numbers (true is 1, null is 0, a non-numeric string is NaN, so
# ``true == 'true'`` is false).
_TOKEN = re.compile(r"\s*(\$\{\{|\}\}|&&|\|\||==|!=|!|\(|\)|'[^']*'|[A-Za-z_][\w.-]*)")
_STATUS_FUNCTIONS = ("always", "success", "failure", "cancelled")


def _gh_number(value):
    if value is None:
        return 0.0
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if text == "":
        return 0.0
    try:
        return float(text)
    except ValueError:
        return float("nan")


def _gh_equal(left, right) -> bool:
    if isinstance(left, str) and isinstance(right, str):
        return left.lower() == right.lower()
    if type(left) is type(right):
        return left == right
    return _gh_number(left) == _gh_number(right)


def _gh_eval(expression: str, context: dict, *, job_if: bool = False):
    tokens = []
    position = 0
    text = expression.strip()
    while position < len(text):
        match = _TOKEN.match(text, position)
        assert match, text[position:]
        tokens.append(match.group(1))
        position = match.end()
    tokens = [token for token in tokens if token not in ("${{", "}}")]
    if job_if and not any(
        token in _STATUS_FUNCTIONS and tokens[index + 1 : index + 2] == ["("]
        for index, token in enumerate(tokens)
    ):
        tokens = ["success", "(", ")", "&&", "(", *tokens, ")"]
    results = [job.get("result") for job in (context.get("needs") or {}).values()]

    def status(name):
        cancelled = bool(context.get("cancelled"))
        if name == "always":
            return True
        if name == "cancelled":
            return cancelled
        if name == "failure":
            return "failure" in results
        return not cancelled and all(result == "success" for result in results)

    def truthy(value):
        return value not in (None, False, "", 0)

    def lookup(path):
        if path in ("true", "false"):
            return path == "true"
        value = context
        for part in path.split("."):
            value = value.get(part) if isinstance(value, dict) else None
        return value

    def parse_or(index):
        left, index = parse_and(index)
        while index < len(tokens) and tokens[index] == "||":
            right, index = parse_and(index + 1)
            left = left if truthy(left) else right
        return left, index

    def parse_and(index):
        left, index = parse_compare(index)
        while index < len(tokens) and tokens[index] == "&&":
            right, index = parse_compare(index + 1)
            left = right if truthy(left) else left
        return left, index

    def parse_compare(index):
        left, index = parse_unary(index)
        while index < len(tokens) and tokens[index] in ("==", "!="):
            operator = tokens[index]
            right, index = parse_unary(index + 1)
            equal = _gh_equal(left, right)
            left = equal if operator == "==" else not equal
        return left, index

    def parse_unary(index):
        if tokens[index] == "!":
            value, index = parse_unary(index + 1)
            return not truthy(value), index
        if tokens[index] == "(":
            value, index = parse_or(index + 1)
            assert tokens[index] == ")"
            return value, index + 1
        token = tokens[index]
        if token.startswith("'"):
            return token[1:-1], index + 1
        if tokens[index + 1 : index + 3] == ["(", ")"]:
            assert token in _STATUS_FUNCTIONS, token
            return status(token), index + 3
        return lookup(token), index + 1

    value, index = parse_or(0)
    assert index == len(tokens), tokens[index:]
    return value


def test_the_expression_evaluator_follows_github_semantics():
    context = {"inputs": {"flag": True}, "needs": {"a": {"result": "failure"}}}
    assert _gh_eval("${{ inputs.flag == 'true' }}", context) is False
    assert _gh_eval("${{ inputs.flag == true }}", context) is True
    assert _gh_eval("${{ 'SUCCESS' == 'success' }}", context) is True
    assert _gh_eval("${{ inputs.missing == '' }}", context) is True
    # No status function: implicitly success() first, so a failed need skips it.
    assert not _gh_eval("${{ inputs.flag }}", context, job_if=True)
    assert _gh_eval("${{ always() && inputs.flag }}", context, job_if=True)
    assert not _gh_eval("${{ !cancelled() }}", {**context, "cancelled": True})


def test_the_expression_evaluator_returns_operands_and_job_statuses():
    context = {
        "inputs": {"name": "beta", "empty": "", "zero": 0, "flag": False},
        "needs": {"a": {"result": "success"}, "b": {"result": "skipped"}},
        "steps": "not-a-mapping",
    }
    # || and && return one of their operands, as GitHub does, not a boolean.
    assert _gh_eval("${{ inputs.empty || 'fallback' }}", context) == "fallback"
    assert _gh_eval("${{ inputs.name || 'fallback' }}", context) == "beta"
    assert _gh_eval("${{ inputs.name && inputs.zero }}", context) == 0
    assert _gh_eval("${{ inputs.flag && inputs.name }}", context) is False
    assert _gh_eval("${{ inputs.name != 'BETA' }}", context) is False
    assert _gh_eval("${{ !(inputs.empty || inputs.zero) }}", context) is True
    assert _gh_eval("${{ steps.x.outputs }}", context) is None
    assert _gh_eval("${{ inputs.missing.deeper }}", context) is None
    assert _gh_eval("false", context) is False
    # success() needs every needed job to succeed; failure() needs one failure.
    assert _gh_eval("${{ success() }}", context) is False
    assert _gh_eval("${{ failure() }}", context) is False
    assert _gh_eval("${{ success() }}", {}) is True
    assert _gh_eval("${{ success() }}", {"cancelled": True}) is False
    assert _gh_eval("${{ cancelled() }}", {"cancelled": True}) is True
    # Any status function in a job if replaces the implicit success().
    assert _gh_eval("${{ inputs.name == 'beta' }}", context, job_if=True) is False
    assert (
        _gh_eval("${{ inputs.name == 'beta' || failure() }}", context, job_if=True)
        is True
    )


@pytest.mark.parametrize(
    "expression",
    ["${{ unknown() }}", "${{ inputs.name inputs.name }}", "${{ inputs.name @ }}"],
    ids=["unknown-function", "trailing-token", "unknown-character"],
)
def test_the_expression_evaluator_refuses_what_it_cannot_read(expression):
    with pytest.raises(AssertionError):
        _gh_eval(expression, {"inputs": {"name": "beta"}})


def _routing_context(**overrides):
    context = {
        "inputs": {"deploy_existing_stack": True, "promote_entrypoint": True},
        "github": {"run_id": "744001", "run_attempt": "1"},
        "needs": {
            "update_routing": {
                "result": "success",
                "outputs": {
                    "before_sha256": "a" * 64,
                    "after_sha256": "b" * 64,
                    "advances_latest": "true",
                },
            },
            "integration": {"result": "success"},
            "authenticated_test": {"result": "success"},
            "promote_entrypoint": {"result": "success"},
        },
    }
    for path, value in overrides.items():
        target = context
        *parents, last = path.split("__")
        for part in parents:
            target = target[part]
        target[last] = value
    return context


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
    finalizer = _workflow_jobs(REUSABLE_DEPLOY_WORKFLOW)["finalize_routing"]
    step = next(
        step
        for step in finalizer["steps"]
        if "--finalize-publication" in step.get("run", "")
    )
    context = _routing_context(**overrides)
    assert bool(_gh_eval(finalizer["if"], context, job_if=True)) is finalizes
    assert str(_gh_eval(step["env"]["GATES_PASSED"], context)).lower() == (gates_passed)


@pytest.mark.parametrize("job", ["integration", "promote_entrypoint"])
def test_a_failing_sentinel_fails_its_step(tmp_path, job):
    """#744 review: `|| true` on the sentinel command must not pass a failure."""
    step = _sentinel_check_step(_workflow_jobs(REUSABLE_DEPLOY_WORKFLOW)[job])
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
    result = _run_bash(
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
    promote = _workflow_jobs(REUSABLE_DEPLOY_WORKFLOW)["promote_entrypoint"]
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
    assert _gh_eval(promote["if"], context, job_if=True)
    context["needs"]["authenticated_test"]["result"] = "failure"
    assert not _gh_eval(promote["if"], context, job_if=True)
    context["needs"]["authenticated_test"]["result"] = "success"
    context["inputs"]["promote_entrypoint"] = False
    assert not _gh_eval(promote["if"], context, job_if=True)


def test_smoke_rejects_a_latest_wrapper_that_lags_alone(tmp_path: Path):
    """#743 review: the wrapper's latest is checked, not only the model's."""
    import copy

    versions = copy.deepcopy(VERSIONS_BODY)
    versions["policyengine"]["latest"] = "6.2.6"

    result = _run_identity_smoke(tmp_path, versions, **IDENTITY)

    assert result.returncode == 1
    assert "latest routes to policyengine 6.2.6 and policyengine-uk 2.126.8" in (
        result.stderr
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
    result = _run_bash(
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
    job = _workflow_jobs(workflow)[job_name]
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
    job = _workflow_jobs(workflow)[job_name]
    assert "continue-on-error" not in job
    assert job.get("defaults", {}).get("run", {}).get("shell") == "bash"
    for step in job["steps"]:
        assert "continue-on-error" not in step
        assert step.get("shell", "bash") == "bash"
        body = step.get("run", "")
        assert not re.search(r"\|\|\s*true\b", body)
        assert not re.search(r"\bset\s+\+e\b", body)


def test_each_publication_is_named_by_its_run_attempt_and_environment():
    routing = _workflow_jobs(REUSABLE_DEPLOY_WORKFLOW)["update_routing"]
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
    jobs = _workflow_jobs(REUSABLE_DEPLOY_WORKFLOW)
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

    cleanup = _job_section(
        REUSABLE_DEPLOY_WORKFLOW.read_text(encoding="utf-8"),
        "cleanup_entrypoint_revisions",
    )
    assert (
        "SIMULATION_ENTRYPOINT_ROLLBACK_REVISION: "
        "${{ needs.deploy_entrypoint.outputs.previous_revision }}"
    ) in cleanup


def test_deployment_ready_requires_the_journal_finalizer_to_complete():
    jobs = _workflow_jobs(REUSABLE_DEPLOY_WORKFLOW)
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
    ready = _workflow_jobs(REUSABLE_DEPLOY_WORKFLOW)["deployment_ready"]
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
    result = _run_bash(
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
        for step in _workflow_jobs(REUSABLE_DEPLOY_WORKFLOW)["deployment_ready"][
            "steps"
        ]
        if step.get("name") == "Check deployment result"
    )
    completed = _run_bash(
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
    jobs = _workflow_jobs(REUSABLE_DEPLOY_WORKFLOW)
    smoke_step = next(
        step
        for step in jobs["integration"]["steps"]
        if "cloud-run-simulation-entry-smoke.sh" in step.get("run", "")
    )
    context = _routing_context(
        inputs__deploy_existing_stack=existing,
        needs__update_routing__outputs__advances_latest=advances,
    )
    for environment in (smoke_step["env"], jobs["promote_entrypoint"]["env"]):
        assert _gh_eval(environment["EXPECTED_LATEST"], context) == expected


def test_traffic_changes_are_exact_revision_validated_and_reversible(tmp_path):
    script = TRAFFIC_SCRIPT.read_text(encoding="utf-8")

    assert "run revisions describe" in script
    assert 'select(.type == "Ready" and .status == "True")' in script
    assert '["serving.knative.dev/service"]' in script
    assert 'run services update-traffic "${service}"' in script
    assert '--to-revisions "${target_revision}=100"' in script
    assert "expected_current_revision" in script
    assert '--to-revisions "LATEST=100"' not in script

    state_file = tmp_path / "active-revision"
    state_file.write_text("policyengine-simulation-entry-00001-old", encoding="utf-8")
    fake_gcloud = tmp_path / "gcloud"
    fake_gcloud.write_text(
        textwrap.dedent("""\
            #!/usr/bin/env bash
            set -euo pipefail
            case "$1 $2 $3" in
              "run services describe")
                active="$(cat "${FAKE_GCLOUD_STATE}")"
                printf '{"status":{"traffic":[{"revisionName":"%s","percent":100}]}}\\n' "${active}"
                ;;
              "run revisions describe")
                revision="$4"
                printf '{"metadata":{"labels":{"serving.knative.dev/service":"policyengine-simulation-entry"}},"status":{"conditions":[{"type":"Ready","status":"True"}]},"revision":"%s"}\\n' "${revision}"
                ;;
              "run services update-traffic")
                target=""
                while [ "$#" -gt 0 ]; do
                  if [ "$1" = "--to-revisions" ]; then
                    target="${2%=100}"
                    break
                  fi
                  shift
                done
                test -n "${target}"
                printf '%s' "${target}" > "${FAKE_GCLOUD_STATE}"
                ;;
              *)
                printf 'Unexpected fake gcloud command: %s\\n' "$*" >&2
                exit 2
                ;;
            esac
            """),  # noqa: E501  # the fake gcloud prints each JSON response on one line, as gcloud does
        encoding="utf-8",
    )
    fake_gcloud.chmod(0o755)

    base_env = {
        **os.environ,
        "GCLOUD_BIN": str(fake_gcloud),
        "FAKE_GCLOUD_STATE": str(state_file),
        "SIMULATION_ENTRYPOINT_GCP_PROJECT_ID": "simulation-entry-test",
        "SIMULATION_ENTRYPOINT_SERVICE": "policyengine-simulation-entry",
    }

    _run_bash(
        TRAFFIC_SCRIPT,
        check=True,
        capture_output=True,
        env={
            **base_env,
            "SIMULATION_ENTRYPOINT_TARGET_REVISION": (
                "policyengine-simulation-entry-00002-new"
            ),
            "SIMULATION_ENTRYPOINT_EXPECTED_CURRENT_REVISION": (
                "policyengine-simulation-entry-00001-old"
            ),
        },
    )
    assert (
        state_file.read_text(encoding="utf-8")
        == "policyengine-simulation-entry-00002-new"
    )

    _run_bash(
        TRAFFIC_SCRIPT,
        check=True,
        capture_output=True,
        env={
            **base_env,
            "SIMULATION_ENTRYPOINT_TARGET_REVISION": (
                "policyengine-simulation-entry-00001-old"
            ),
            "SIMULATION_ENTRYPOINT_EXPECTED_CURRENT_REVISION": (
                "policyengine-simulation-entry-00002-new"
            ),
        },
    )
    assert (
        state_file.read_text(encoding="utf-8")
        == "policyengine-simulation-entry-00001-old"
    )


def test_traffic_change_refuses_an_intervening_promotion(tmp_path):
    state_file = tmp_path / "active-revision"
    state_file.write_text(
        "policyengine-simulation-entry-00003-intervening",
        encoding="utf-8",
    )
    fake_gcloud = tmp_path / "gcloud"
    fake_gcloud.write_text(
        textwrap.dedent("""\
            #!/usr/bin/env bash
            set -euo pipefail
            active="$(cat "${FAKE_GCLOUD_STATE}")"
            printf '{"status":{"traffic":[{"revisionName":"%s","percent":100}]}}\\n' "${active}"
            """),  # noqa: E501  # the fake gcloud prints its JSON response on one line, as gcloud does
        encoding="utf-8",
    )
    fake_gcloud.chmod(0o755)

    result = _run_bash(
        TRAFFIC_SCRIPT,
        check=False,
        capture_output=True,
        env={
            **os.environ,
            "GCLOUD_BIN": str(fake_gcloud),
            "FAKE_GCLOUD_STATE": str(state_file),
            "SIMULATION_ENTRYPOINT_GCP_PROJECT_ID": "simulation-entry-test",
            "SIMULATION_ENTRYPOINT_SERVICE": "policyengine-simulation-entry",
            "SIMULATION_ENTRYPOINT_TARGET_REVISION": (
                "policyengine-simulation-entry-00002-new"
            ),
            "SIMULATION_ENTRYPOINT_EXPECTED_CURRENT_REVISION": (
                "policyengine-simulation-entry-00001-old"
            ),
        },
    )

    assert result.returncode == 2
    assert "Stable traffic changed after deployment" in result.stderr


def test_container_context_excludes_local_environments_and_unrelated_projects():
    ignore_rules = DOCKERIGNORE.read_text(encoding="utf-8")
    gitignore_rules = GITIGNORE.read_text(encoding="utf-8")

    assert "gha-creds-*.json" in ignore_rules
    assert "gha-creds-*.json" in gitignore_rules
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


def test_production_lock_contains_the_direct_stage12_database_runtime():
    lockfile = tomllib.loads(
        (
            REPOSITORY_ROOT / "projects" / "policyengine-simulation-entry" / "uv.lock"
        ).read_text(encoding="utf-8")
    )
    resolved_packages = {package["name"] for package in lockfile["package"]}

    assert {
        "httpx",
        "modal",
        "policyengine-stage12-persistence",
        "psycopg",
        "sqlalchemy",
    } <= resolved_packages
    for package in ("policyengine-fastapi", "sqlmodel"):
        assert package not in resolved_packages


def test_client_publication_checks_out_the_deployed_commit():
    workflow = PUBLISH_WORKFLOW.read_text(encoding="utf-8")

    assert "github.event.workflow_run.head_sha" in workflow


# The gateway's real /versions response for two published releases (6.2.7
# latest); test_versions_fixture.py in the gateway project keeps it current.
VERSIONS_BODY = json.loads(
    (Path(__file__).parent / "fixtures" / "versions_body.json").read_text()
)


def _run_identity_smoke(tmp_path: Path, versions: dict, **expected: str):
    import json

    revision = "policyengine-simulation-entry-00002-new"
    state_file, revisions_file = _write_fake_smoke_curl(tmp_path, [revision])
    versions_file = tmp_path / "versions.json"
    versions_file.write_text(json.dumps(versions), encoding="utf-8")
    env = {
        **_smoke_test_env(tmp_path, state_file, revisions_file),
        "FAKE_CURL_VERSIONS": str(versions_file),
        **expected,
    }
    return _run_bash(
        SMOKE_SCRIPT,
        "https://simulation.example.test",
        revision,
        check=False,
        capture_output=True,
        env=env,
    )


IDENTITY = {
    "EXPECTED_IDENTITY": "required",
    "EXPECTED_POLICYENGINE_VERSION": "6.2.7",
    "EXPECTED_APP_NAME": "policyengine-simulation-py6-2-7",
    "EXPECTED_UK_MODEL_VERSION": "2.126.8",
    "EXPECTED_UK_DATASET_SHA256": "a" * 64,
    "EXPECTED_LATEST": "true",
}


def test_smoke_accepts_the_gateway_identity_of_a_new_release(tmp_path: Path):
    result = _run_identity_smoke(tmp_path, VERSIONS_BODY, **IDENTITY)

    assert result.returncode == 0, result.stderr
    assert (
        "serves policyengine 6.2.7 from policyengine-simulation-py6-2-7, "
        "policyengine-uk 2.126.8, UK dataset " + "a" * 64 + " as latest"
    ) in result.stdout


def test_smoke_accepts_an_older_release_by_its_route(tmp_path: Path):
    result = _run_identity_smoke(
        tmp_path,
        VERSIONS_BODY,
        EXPECTED_IDENTITY="required",
        EXPECTED_POLICYENGINE_VERSION="6.2.6",
        EXPECTED_APP_NAME="policyengine-simulation-py6-2-6",
        EXPECTED_UK_MODEL_VERSION="2.125.1",
        EXPECTED_UK_DATASET_SHA256="5" * 64,
    )

    assert result.returncode == 0, result.stderr


def test_smoke_without_expectations_only_requires_versions_json(tmp_path: Path):
    result = _run_identity_smoke(tmp_path, {})

    assert result.returncode == 0, result.stderr


def test_smoke_rejects_latest_routing_to_another_version(tmp_path: Path):
    import copy

    versions = copy.deepcopy(VERSIONS_BODY)
    versions["policyengine"]["latest"] = "6.2.6"
    versions["uk"]["latest"] = "2.125.1"

    result = _run_identity_smoke(tmp_path, versions, **IDENTITY)

    assert result.returncode == 1
    assert "latest routes to policyengine 6.2.6 and policyengine-uk 2.125.1" in (
        result.stderr
    )


def test_smoke_rejects_a_uk_latest_that_lags_the_wrapper(tmp_path: Path):
    import copy

    versions = copy.deepcopy(VERSIONS_BODY)
    versions["uk"]["latest"] = "2.125.1"

    result = _run_identity_smoke(tmp_path, versions, **IDENTITY)

    assert result.returncode == 1
    assert "latest routes to" in result.stderr


def test_smoke_rejects_a_release_without_a_route(tmp_path: Path):
    import copy

    versions = copy.deepcopy(VERSIONS_BODY)
    del versions["policyengine"]["6.2.7"]

    result = _run_identity_smoke(tmp_path, versions, **IDENTITY)

    assert result.returncode == 1
    assert "no route and UK bundle identity for policyengine 6.2.7" in result.stderr


def test_smoke_rejects_a_uk_model_routed_to_another_app(tmp_path: Path):
    import copy

    versions = copy.deepcopy(VERSIONS_BODY)
    versions["uk"]["2.126.8"] = "policyengine-simulation-py6-2-6"

    result = _run_identity_smoke(tmp_path, versions, **IDENTITY)

    assert result.returncode == 1
    assert (
        "policyengine-uk 2.126.8 routes to policyengine-simulation-py6-2-6, "
        "expected policyengine-simulation-py6-2-7"
    ) in result.stderr


def test_smoke_rejects_another_uk_dataset(tmp_path: Path):
    result = _run_identity_smoke(
        tmp_path, VERSIONS_BODY, **{**IDENTITY, "EXPECTED_UK_DATASET_SHA256": "b" * 64}
    )

    assert result.returncode == 1
    assert "UK dataset SHA-256" in result.stderr


def test_smoke_rejects_identity_expectations_without_a_version(tmp_path: Path):
    result = _run_identity_smoke(
        tmp_path, VERSIONS_BODY, EXPECTED_UK_DATASET_SHA256="a" * 64
    )

    assert result.returncode == 2
    assert "EXPECTED_POLICYENGINE_VERSION is required" in result.stderr


def test_smoke_accepts_an_unrecorded_digest_only_when_none_is_served(
    tmp_path: Path,
):
    """A release published before digests were recorded serves none."""
    import copy

    versions = copy.deepcopy(VERSIONS_BODY)
    del versions["bundles"]["6.2.7"]["uk"]["dataset_sha256"]
    expected = {**IDENTITY, "EXPECTED_UK_DATASET_SHA256": "unrecorded"}
    (tmp_path / "unrecorded").mkdir()
    (tmp_path / "served").mkdir()
    result = _run_identity_smoke(tmp_path / "unrecorded", versions, **expected)
    assert result.returncode == 0, result.stderr
    assert "UK dataset unrecorded as latest" in result.stdout
    served = _run_identity_smoke(tmp_path / "served", VERSIONS_BODY, **expected)
    assert served.returncode == 1
    assert "is " + "a" * 64 + ", expected unrecorded" in served.stderr


def test_smoke_rejects_worker_routes_to_an_old_app(tmp_path: Path):
    """#743 round-2 review: the version and its model route to the new app."""
    import copy

    versions = copy.deepcopy(VERSIONS_BODY)
    versions["policyengine"]["6.2.7"] = "policyengine-simulation-py6-2-6"
    versions["uk"]["2.126.8"] = "policyengine-simulation-py6-2-6"

    result = _run_identity_smoke(tmp_path, versions, **IDENTITY)

    assert result.returncode == 1
    assert (
        "policyengine 6.2.7 routes to policyengine-simulation-py6-2-6, "
        "expected policyengine-simulation-py6-2-7"
    ) in result.stderr


@pytest.mark.parametrize(
    "missing",
    [
        "EXPECTED_POLICYENGINE_VERSION",
        "EXPECTED_APP_NAME",
        "EXPECTED_UK_MODEL_VERSION",
        "EXPECTED_UK_DATASET_SHA256",
    ],
)
@pytest.mark.parametrize("how", ["unset", "empty"])
def test_smoke_refuses_an_incomplete_identity(tmp_path: Path, missing, how):
    """#743 round-2 review: an absent expectation never weakens the gate."""
    expected = dict(IDENTITY)
    if how == "unset":
        del expected[missing]
    else:
        expected[missing] = ""

    result = _run_identity_smoke(tmp_path, VERSIONS_BODY, **expected)

    assert result.returncode == 2
    assert f"{missing} is required" in result.stderr


def test_smoke_requires_every_expectation_when_identity_is_required(tmp_path: Path):
    result = _run_identity_smoke(tmp_path, VERSIONS_BODY, EXPECTED_IDENTITY="required")

    assert result.returncode == 2
    assert "is required" in result.stderr


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("EXPECTED_IDENTITY", "optional"),
        ("EXPECTED_LATEST", "yes"),
        ("EXPECTED_UK_DATASET_SHA256", "A" * 64),
        ("EXPECTED_UK_DATASET_SHA256", "a" * 63),
    ],
)
def test_smoke_refuses_malformed_expectations(tmp_path: Path, name, value):
    result = _run_identity_smoke(tmp_path, VERSIONS_BODY, **{**IDENTITY, name: value})

    assert result.returncode == 2


def test_smoke_rejects_a_bundle_recorded_for_another_wrapper(tmp_path: Path):
    """#743 round-2 review: the bundle must be the expected version's own."""
    import copy

    versions = copy.deepcopy(VERSIONS_BODY)
    versions["bundles"]["6.2.7"]["uk"]["policyengine_version"] = "6.2.6"

    result = _run_identity_smoke(tmp_path, versions, **IDENTITY)

    assert result.returncode == 1
    assert "no route and UK bundle identity for policyengine 6.2.7" in result.stderr


def _workflow_jobs(path: Path) -> dict:
    import yaml

    return yaml.safe_load(path.read_text(encoding="utf-8"))["jobs"]


def test_deploy_gates_pass_the_release_identity_to_the_smoke():
    """Review M2: the active workflow (not comments) wires the whole identity."""
    jobs = _workflow_jobs(REUSABLE_DEPLOY_WORKFLOW)
    assert (
        jobs["prepare"]["outputs"]["uk_dataset_sha256"]
        == "${{ steps.versions.outputs.uk_dataset_sha256 }}"
    )
    assert (
        jobs["deploy_executor"]["outputs"]["app_name"]
        == "${{ steps.app.outputs.name }}"
    )
    smoke_step = next(
        step
        for step in jobs["integration"]["steps"]
        if "cloud-run-simulation-entry-smoke.sh" in step.get("run", "")
    )
    expected = {
        "EXPECTED_IDENTITY": (
            "${{ inputs.deploy_existing_stack && 'required' || 'none' }}"
        ),
        "EXPECTED_APP_NAME": (
            "${{ inputs.deploy_existing_stack && "
            "needs.deploy_executor.outputs.app_name || '' }}"
        ),
        "EXPECTED_POLICYENGINE_VERSION": (
            "${{ inputs.deploy_existing_stack && "
            "needs.prepare.outputs.policyengine_version || '' }}"
        ),
        "EXPECTED_UK_MODEL_VERSION": (
            "${{ inputs.deploy_existing_stack && "
            "needs.prepare.outputs.uk_version || '' }}"
        ),
        "EXPECTED_UK_DATASET_SHA256": (
            "${{ inputs.deploy_existing_stack && "
            "needs.prepare.outputs.uk_dataset_sha256 || '' }}"
        ),
        # Latest must route to this release only when it should advance it.
        "EXPECTED_LATEST": (
            "${{ inputs.deploy_existing_stack && "
            "needs.update_routing.outputs.advances_latest == 'true' && 'true' || '' }}"
        ),
    }
    promote = jobs["promote_entrypoint"]
    for environment in (smoke_step["env"], promote["env"]):
        assert {key: environment.get(key) for key in expected} == expected
    for job in ("integration", "promote_entrypoint"):
        assert {"prepare", "deploy_executor", "update_routing"} <= set(
            jobs[job]["needs"]
        )
    # The promotion smoke runs inherit the job environment; no step overrides it.
    for step in promote["steps"]:
        if "cloud-run-simulation-entry-smoke.sh" in step.get("run", ""):
            assert not set(step.get("env") or {}) & set(expected)
    updates = _workflow_jobs(
        REPOSITORY_ROOT / ".github" / "workflows" / "check-policyengine-updates.yml"
    )
    update_envs = [
        step.get("env") or {}
        for job in updates.values()
        for step in job.get("steps", [])
    ]
    assert any(
        env.get("RELEASE_PAYLOAD") == "${{ toJSON(github.event.client_payload) }}"
        for env in update_envs
    )


def test_smoke_rejects_another_uk_model(tmp_path: Path):
    result = _run_identity_smoke(
        tmp_path, VERSIONS_BODY, **{**IDENTITY, "EXPECTED_UK_MODEL_VERSION": "2.125.1"}
    )

    assert result.returncode == 1
    assert "records policyengine-uk 2.126.8, expected 2.125.1" in result.stderr


def test_smoke_rejects_a_missing_bundle_identity(tmp_path: Path):
    import copy

    versions = copy.deepcopy(VERSIONS_BODY)
    versions["bundles"] = {}

    result = _run_identity_smoke(tmp_path, versions, **IDENTITY)

    assert result.returncode == 1
    assert "no route and UK bundle identity for policyengine 6.2.7" in result.stderr
