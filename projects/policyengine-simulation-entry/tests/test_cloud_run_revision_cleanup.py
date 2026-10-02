from __future__ import annotations

import json
import os
import subprocess
import textwrap
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
CLEANUP_SCRIPT = (
    REPOSITORY_ROOT
    / ".github"
    / "scripts"
    / "cleanup-cloud-run-simulation-entry-revisions.sh"
)
REUSABLE_DEPLOY_WORKFLOW = (
    REPOSITORY_ROOT / ".github" / "workflows" / "simulation-deploy.reusable.yml"
)


def _revision(name: str, created_at: str, *, ready: bool = True) -> dict:
    return {
        "metadata": {
            "name": name,
            "creationTimestamp": created_at,
        },
        "status": {
            "conditions": [
                {
                    "type": "Ready",
                    "status": "True" if ready else "False",
                }
            ]
        },
    }


def _write_fake_gcloud(tmp_path: Path) -> Path:
    fake_gcloud = tmp_path / "gcloud"
    fake_gcloud.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            set -euo pipefail

            case "$1 $2 $3" in
              "run services describe")
                cat "${FAKE_SERVICE_JSON}"
                ;;
              "run revisions list")
                cat "${FAKE_REVISIONS_JSON}"
                ;;
              "run services update-traffic")
                printf '%s\n' "$@" > "${FAKE_UPDATE_ARGUMENTS}"
                ;;
              "run revisions delete")
                printf '%s\n' "$4" >> "${FAKE_DELETED_REVISIONS}"
                ;;
              *)
                printf 'Unexpected fake gcloud command: %s\n' "$*" >&2
                exit 2
                ;;
            esac
            """
        ),
        encoding="utf-8",
    )
    fake_gcloud.chmod(0o755)
    return fake_gcloud


def _run_cleanup(
    tmp_path: Path,
    *,
    service_traffic: list[dict],
    revisions: list[dict],
    successful_revision: str,
    previous_revision: str = "",
) -> subprocess.CompletedProcess[str]:
    service_json = tmp_path / "service.json"
    revisions_json = tmp_path / "revisions.json"
    update_arguments = tmp_path / "update-arguments.txt"
    deleted_revisions = tmp_path / "deleted-revisions.txt"
    service_json.write_text(
        json.dumps({"status": {"traffic": service_traffic}}),
        encoding="utf-8",
    )
    revisions_json.write_text(json.dumps(revisions), encoding="utf-8")

    return subprocess.run(
        ["bash", CLEANUP_SCRIPT],
        check=False,
        capture_output=True,
        text=True,
        env=os.environ
        | {
            "GCLOUD_BIN": str(_write_fake_gcloud(tmp_path)),
            "FAKE_SERVICE_JSON": str(service_json),
            "FAKE_REVISIONS_JSON": str(revisions_json),
            "FAKE_UPDATE_ARGUMENTS": str(update_arguments),
            "FAKE_DELETED_REVISIONS": str(deleted_revisions),
            "SIMULATION_ENTRYPOINT_GCP_PROJECT_ID": "simulation-entry-test",
            "SIMULATION_ENTRYPOINT_GCP_REGION": "us-central1",
            "SIMULATION_ENTRYPOINT_SERVICE": "policyengine-simulation-entry",
            "SIMULATION_ENTRYPOINT_SUCCESSFUL_REVISION": successful_revision,
            "SIMULATION_ENTRYPOINT_PREVIOUS_SUCCESSFUL_REVISION": previous_revision,
            "SIMULATION_ENTRYPOINT_REVISIONS_TO_RETAIN": "3",
        },
    )


def test_cleanup_retains_three_successful_revisions_and_deletes_older_ones(
    tmp_path: Path,
):
    revisions = [
        _revision("entry-00003-three", "2026-09-03T00:00:00Z"),
        _revision("entry-00005-five", "2026-09-05T00:00:00Z"),
        _revision("entry-00001-one", "2026-09-01T00:00:00Z"),
        _revision("entry-00004-four", "2026-09-04T00:00:00Z"),
        _revision("entry-00002-two", "2026-09-02T00:00:00Z"),
    ]
    traffic = [
        {"revisionName": "entry-00005-five", "percent": 100},
        {"revisionName": "entry-00001-one", "tag": "p-1"},
        {"revisionName": "entry-00002-two", "tag": "p-2"},
        {"revisionName": "entry-00003-three", "tag": "p-3"},
        {"revisionName": "entry-00004-four", "tag": "p-4"},
        {"revisionName": "entry-00005-five", "tag": "p-5"},
    ]

    result = _run_cleanup(
        tmp_path,
        service_traffic=traffic,
        revisions=revisions,
        successful_revision="entry-00005-five",
        previous_revision="entry-00004-four",
    )

    assert result.returncode == 0, result.stderr
    update_arguments = (tmp_path / "update-arguments.txt").read_text(encoding="utf-8")
    tag_argument = update_arguments.splitlines()[
        update_arguments.splitlines().index("--set-tags") + 1
    ]
    assert "p-5=entry-00005-five" in tag_argument
    assert "p-4=entry-00004-four" in tag_argument
    assert "p-3=entry-00003-three" in tag_argument
    assert "pe-retained-1=entry-00005-five" in tag_argument
    assert "pe-retained-2=entry-00004-four" in tag_argument
    assert "pe-retained-3=entry-00003-three" in tag_argument
    assert "p-1=" not in tag_argument
    assert "p-2=" not in tag_argument
    assert "--to-revisions" not in update_arguments
    assert (tmp_path / "deleted-revisions.txt").read_text(
        encoding="utf-8"
    ).splitlines() == ["entry-00002-two", "entry-00001-one"]


def test_cleanup_uses_prior_retention_order_before_ready_revision_fallback(
    tmp_path: Path,
):
    revisions = [
        _revision("entry-00006-six", "2026-09-06T00:00:00Z"),
        _revision("entry-00005-five", "2026-09-05T00:00:00Z"),
        _revision("entry-00004-four", "2026-09-04T00:00:00Z"),
        _revision("entry-00003-three", "2026-09-03T00:00:00Z"),
    ]
    traffic = [
        {"revisionName": "entry-00006-six", "percent": 100},
        {"revisionName": "entry-00006-six", "tag": "p-6"},
        {"revisionName": "entry-00005-five", "tag": "p-5"},
        {"revisionName": "entry-00004-four", "tag": "p-4"},
        {"revisionName": "entry-00005-five", "tag": "pe-retained-1"},
        {"revisionName": "entry-00004-four", "tag": "pe-retained-2"},
        {"revisionName": "entry-00003-three", "tag": "pe-retained-3"},
    ]

    result = _run_cleanup(
        tmp_path,
        service_traffic=traffic,
        revisions=revisions,
        successful_revision="entry-00006-six",
        previous_revision="entry-00005-five",
    )

    assert result.returncode == 0, result.stderr
    update_arguments = (tmp_path / "update-arguments.txt").read_text(encoding="utf-8")
    assert "pe-retained-1=entry-00006-six" in update_arguments
    assert "pe-retained-2=entry-00005-five" in update_arguments
    assert "pe-retained-3=entry-00004-four" in update_arguments
    assert (tmp_path / "deleted-revisions.txt").read_text(
        encoding="utf-8"
    ).splitlines() == ["entry-00003-three"]


def test_cleanup_refuses_to_mutate_when_successful_revision_is_not_ready(
    tmp_path: Path,
):
    result = _run_cleanup(
        tmp_path,
        service_traffic=[{"revisionName": "entry-00001-one", "percent": 100}],
        revisions=[
            _revision("entry-00002-two", "2026-09-02T00:00:00Z", ready=False),
            _revision("entry-00001-one", "2026-09-01T00:00:00Z"),
        ],
        successful_revision="entry-00002-two",
        previous_revision="entry-00001-one",
    )

    assert result.returncode == 1
    assert "is not a ready revision" in result.stderr
    assert not (tmp_path / "update-arguments.txt").exists()
    assert not (tmp_path / "deleted-revisions.txt").exists()


def test_cleanup_preserves_a_traffic_revision_for_an_unpromoted_environment(
    tmp_path: Path,
):
    revisions = [
        _revision("entry-00004-four", "2026-09-04T00:00:00Z"),
        _revision("entry-00003-three", "2026-09-03T00:00:00Z"),
        _revision("entry-00002-two", "2026-09-02T00:00:00Z"),
        _revision("entry-00001-one", "2026-09-01T00:00:00Z"),
    ]
    traffic = [
        {"revisionName": "entry-00001-one", "percent": 100},
        {"revisionName": "entry-00004-four", "tag": "s-4"},
        {"revisionName": "entry-00003-three", "tag": "pe-retained-1"},
    ]

    result = _run_cleanup(
        tmp_path,
        service_traffic=traffic,
        revisions=revisions,
        successful_revision="entry-00004-four",
    )

    assert result.returncode == 0, result.stderr
    update_arguments = (tmp_path / "update-arguments.txt").read_text(encoding="utf-8")
    assert "pe-retained-1=entry-00004-four" in update_arguments
    assert "pe-retained-2=entry-00001-one" in update_arguments
    assert "pe-retained-3=entry-00003-three" in update_arguments
    assert (tmp_path / "deleted-revisions.txt").read_text(
        encoding="utf-8"
    ).splitlines() == ["entry-00002-two"]


def test_revision_cleanup_runs_after_successful_deployment_completion():
    workflow = REUSABLE_DEPLOY_WORKFLOW.read_text(encoding="utf-8")

    assert workflow.index("\n  deployment_ready:") < workflow.index(
        "\n  cleanup_entrypoint_revisions:"
    )
    cleanup_job = workflow[workflow.index("\n  cleanup_entrypoint_revisions:") :]
    assert "needs: [deployment_ready, deploy_entrypoint]" in cleanup_job
    assert "needs.deployment_ready.result == 'success'" in cleanup_job
    assert "id-token: write" in cleanup_job
    assert 'SIMULATION_ENTRYPOINT_REVISIONS_TO_RETAIN: "3"' in cleanup_job
    assert "cleanup-cloud-run-simulation-entry-revisions.sh" in cleanup_job


def test_revision_cleanup_script_has_valid_shell_syntax_and_is_executable():
    subprocess.run(["bash", "-n", CLEANUP_SCRIPT], check=True)
    assert os.access(CLEANUP_SCRIPT, os.X_OK)
