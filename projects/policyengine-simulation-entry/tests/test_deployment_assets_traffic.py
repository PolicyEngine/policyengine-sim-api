"""Tests for traffic changes, the container context, the lock and client publication."""

from __future__ import annotations

import os
import textwrap

import tomllib
from test_deployment_assets_support import (
    DOCKERIGNORE,
    GITIGNORE,
    PUBLISH_WORKFLOW,
    REPOSITORY_ROOT,
    TRAFFIC_SCRIPT,
    run_bash,
)


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

    run_bash(
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

    run_bash(
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

    result = run_bash(
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
