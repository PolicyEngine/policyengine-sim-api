from __future__ import annotations

import json
import os
import re
import subprocess
import textwrap
from pathlib import Path

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
DEPLOY_WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "simulation-deploy.yml"
REUSABLE_DEPLOY_WORKFLOW = (
    REPOSITORY_ROOT / ".github" / "workflows" / "simulation-deploy.reusable.yml"
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


def test_deployment_scripts_have_valid_shell_syntax():
    subprocess.run(["bash", "-n", TRAFFIC_SCRIPT], check=True)
    subprocess.run(["bash", "-n", SMOKE_SCRIPT], check=True)
    subprocess.run(["bash", "-n", CLOUD_RUN_DEPLOY_SCRIPT], check=True)
    assert os.access(SMOKE_SCRIPT, os.X_OK)
    assert os.access(CLOUD_RUN_DEPLOY_SCRIPT, os.X_OK)


def test_stage12_deployment_uses_the_api_owned_persistence_service():
    validation_script = STAGE12_VALIDATION_SCRIPT.read_text(encoding="utf-8")

    assert "STAGE12_PERSISTENCE_API_URL" in validation_script
    assert "STAGE12_DATABASE_URL" not in validation_script
    assert "psycopg" not in validation_script


def test_stage12_runtime_contains_no_direct_database_adapter_or_credential() -> None:
    removed_adapter = (
        REPOSITORY_ROOT
        / "libs"
        / "policyengine-simulation-contract"
        / "src"
        / "policyengine_simulation_contract"
        / "stage12_persistence.py"
    )
    assert not removed_adapter.exists()

    inspected_paths = (
        REPOSITORY_ROOT / ".github" / "scripts",
        REPOSITORY_ROOT / ".github" / "workflows",
        REPOSITORY_ROOT / "libs" / "policyengine-stage12-client" / "src",
        REPOSITORY_ROOT / "projects" / "policyengine-simulation-entry" / "src",
        REPOSITORY_ROOT / "projects" / "policyengine-simulation-executor" / "src",
    )
    for root in inspected_paths:
        for path in root.rglob("*"):
            if path.suffix not in {".py", ".sh", ".yml", ".yaml"}:
                continue
            source = path.read_text(encoding="utf-8")
            assert "STAGE12_DATABASE_URL" not in source, path
            assert "PostgresComparisonStore" not in source, path


def test_deployment_uses_gcloud_workflow_without_terraform():
    deploy_workflow = DEPLOY_WORKFLOW.read_text(encoding="utf-8")
    reusable_workflow = REUSABLE_DEPLOY_WORKFLOW.read_text(encoding="utf-8")
    stage12_workflow = STAGE12_DEPLOY_WORKFLOW.read_text(encoding="utf-8")
    cloud_run_deploy_script = CLOUD_RUN_DEPLOY_SCRIPT.read_text(encoding="utf-8")

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
    assert (
        reusable_workflow.count("environment: ${{ inputs.release_environment }}") == 11
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


def test_cloud_run_deployment_escapes_environment_and_pins_secret_versions(tmp_path):
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
    arguments_capture = tmp_path / "gcloud-arguments.txt"
    env_capture = tmp_path / "runtime-environment.json"
    command_environment = os.environ | {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "RUNNER_TEMP": str(tmp_path),
        "GCLOUD_ARGUMENTS_CAPTURE": str(arguments_capture),
        "GCLOUD_ENV_FILE_CAPTURE": str(env_capture),
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
        "STAGE12_ENABLED_VALUE": "0",
        "STAGE12_ARTIFACT_BUCKET_VALUE": "policyengine-stage12-staging",
        "STAGE12_PERSISTENCE_API_URL_VALUE": "https://api.example",
        "MODAL_TOKEN_ID_SECRET_NAME": "modal-token-id",
        "MODAL_TOKEN_SECRET_SECRET_NAME": "modal-token-secret",
    }

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
    assert runtime_environment["STAGE12_PERSISTENCE_API_URL"] == ("https://api.example")
    arguments = arguments_capture.read_text(encoding="utf-8").splitlines()
    secrets_argument = arguments[arguments.index("--set-secrets") + 1]
    assert secrets_argument == (
        "OLD_GATEWAY_AUTH_CLIENT_SECRET=old-client-secret:3,"
        "MODAL_TOKEN_ID=modal-token-id:3,"
        "MODAL_TOKEN_SECRET=modal-token-secret:3"
    )
    assert ":latest" not in secrets_argument


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
    assert "needs: [deploy_entrypoint, authenticated_test]" in reusable_workflow
    assert (
        'cloud-run-simulation-entry-smoke.sh "${STABLE_URL}" "${TARGET_REVISION}"'
        in reusable_workflow
    )
    assert "failure() && steps.promote.outcome == 'success'" in reusable_workflow
    assert "needs: beta" in deploy_workflow
    assert "skip_beta" not in deploy_workflow


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

    stage12_start = reusable_workflow.index("\n  deploy_stage12_v2:")
    stage12_section = reusable_workflow[stage12_start:]
    assert "src.modal.utils.update_version_registry" not in stage12_section


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
            """),
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

    subprocess.run(
        ["bash", TRAFFIC_SCRIPT],
        check=True,
        capture_output=True,
        text=True,
        env={
            **base_env,
            "SIMULATION_ENTRYPOINT_TARGET_REVISION": "policyengine-simulation-entry-00002-new",
            "SIMULATION_ENTRYPOINT_EXPECTED_CURRENT_REVISION": "policyengine-simulation-entry-00001-old",
        },
    )
    assert (
        state_file.read_text(encoding="utf-8")
        == "policyengine-simulation-entry-00002-new"
    )

    subprocess.run(
        ["bash", TRAFFIC_SCRIPT],
        check=True,
        capture_output=True,
        text=True,
        env={
            **base_env,
            "SIMULATION_ENTRYPOINT_TARGET_REVISION": "policyengine-simulation-entry-00001-old",
            "SIMULATION_ENTRYPOINT_EXPECTED_CURRENT_REVISION": "policyengine-simulation-entry-00002-new",
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
            """),
        encoding="utf-8",
    )
    fake_gcloud.chmod(0o755)

    result = subprocess.run(
        ["bash", TRAFFIC_SCRIPT],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GCLOUD_BIN": str(fake_gcloud),
            "FAKE_GCLOUD_STATE": str(state_file),
            "SIMULATION_ENTRYPOINT_GCP_PROJECT_ID": "simulation-entry-test",
            "SIMULATION_ENTRYPOINT_SERVICE": "policyengine-simulation-entry",
            "SIMULATION_ENTRYPOINT_TARGET_REVISION": "policyengine-simulation-entry-00002-new",
            "SIMULATION_ENTRYPOINT_EXPECTED_CURRENT_REVISION": "policyengine-simulation-entry-00001-old",
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


def test_production_lock_contains_only_the_required_stage12_runtime_clients():
    lockfile = tomllib.loads(
        (
            REPOSITORY_ROOT / "projects" / "policyengine-simulation-entry" / "uv.lock"
        ).read_text(encoding="utf-8")
    )
    resolved_packages = {package["name"] for package in lockfile["package"]}

    assert {"google-auth", "httpx", "modal", "policyengine-stage12-client"} <= (
        resolved_packages
    )
    assert "psycopg" not in resolved_packages
    for package in ("policyengine-fastapi", "sqlalchemy", "sqlmodel"):
        assert package not in resolved_packages


def test_client_publication_checks_out_the_deployed_commit():
    workflow = PUBLISH_WORKFLOW.read_text(encoding="utf-8")

    assert "github.event.workflow_run.head_sha" in workflow
