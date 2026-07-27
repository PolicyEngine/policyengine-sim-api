from __future__ import annotations

import os
import subprocess
import textwrap
import tomllib
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
TRAFFIC_SCRIPT = (
    REPOSITORY_ROOT
    / ".github"
    / "scripts"
    / "set-cloud-run-simulation-entry-revision.sh"
)
SMOKE_SCRIPT = (
    REPOSITORY_ROOT / ".github" / "scripts" / "cloud-run-simulation-entry-smoke.sh"
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


def test_deployment_scripts_have_valid_shell_syntax():
    subprocess.run(["bash", "-n", TRAFFIC_SCRIPT], check=True)
    subprocess.run(["bash", "-n", SMOKE_SCRIPT], check=True)
    assert os.access(SMOKE_SCRIPT, os.X_OK)


def test_deployment_uses_gcloud_workflow_without_terraform():
    deploy_workflow = DEPLOY_WORKFLOW.read_text(encoding="utf-8")
    reusable_workflow = REUSABLE_DEPLOY_WORKFLOW.read_text(encoding="utf-8")

    assert "gcloud run deploy" in reusable_workflow
    assert "set-cloud-run-simulation-entry-revision.sh" in reusable_workflow
    assert "terraform" not in reusable_workflow.lower()
    assert "deploy_entrypoint:" in reusable_workflow
    assert "deploy_gateway:" in reusable_workflow
    assert "deploy_executor:" in reusable_workflow
    assert "update_routing:" in reusable_workflow
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
    assert "promote_entrypoint: false" in deploy_workflow
    assert "promote_entrypoint: true" in deploy_workflow
    assert "entrypoint_environment" not in deploy_workflow
    assert "entrypoint_environment" not in reusable_workflow
    assert "entrypoint_public_url" not in deploy_workflow
    assert "entrypoint_public_url" not in reusable_workflow
    assert "SIMULATION_ENTRYPOINT_PUBLIC_URL" not in reusable_workflow
    assert "staging.simulation.api.policyengine.org" not in deploy_workflow
    assert (
        reusable_workflow.count("environment: ${{ inputs.release_environment }}") == 8
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
    assert not (
        REPOSITORY_ROOT
        / ".github"
        / "scripts"
        / "configure-cloud-run-simulation-entry-domains.sh"
    ).exists()


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
        textwrap.dedent(
            """\
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
            """
        ),
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
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            set -euo pipefail
            active="$(cat "${FAKE_GCLOUD_STATE}")"
            printf '{"status":{"traffic":[{"revisionName":"%s","percent":100}]}}\\n' "${active}"
            """
        ),
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


def test_client_publication_checks_out_the_deployed_commit():
    workflow = PUBLISH_WORKFLOW.read_text(encoding="utf-8")

    assert "github.event.workflow_run.head_sha" in workflow
