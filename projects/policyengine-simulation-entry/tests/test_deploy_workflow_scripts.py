"""Tests for the scripts the simulation deployment workflows run.

Each test runs a script with fake gcloud or uv commands first on PATH (jq is
real) and asserts on the exact calls, $GITHUB_OUTPUT and exit status, so the
production steps keep behaving as the inline shell they replaced.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from workflow_script_support import (
    WORKFLOWS_DIR,
    FakeCommands,
    run_script,
)

REUSABLE_DEPLOY_WORKFLOW = WORKFLOWS_DIR / "simulation-deploy.reusable.yml"


@pytest.fixture
def fakes(tmp_path: Path) -> FakeCommands:
    return FakeCommands.create(tmp_path)


def _step(workflow: str, name: str) -> str:
    """The text of one workflow step, from its ``- name:`` to the next step."""
    match = re.search(
        rf"^(?P<indent> +)- name: {re.escape(name)}\n(?P<body>.*?)(?=^(?P=indent)- |^  \S|\Z)",
        workflow,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert match is not None, name
    return match.group("body")


def _output(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


# --- deploy_entrypoint: Cloud Run state -----------------------------------

STABLE_STATE = "cloud-run-simulation-entry-stable-state.sh"
CANDIDATE = "cloud-run-simulation-entry-candidate.sh"
SERVICE = "policyengine-simulation-entry-staging"
PROJECT = "policyengine-api"
REGION = "us-central1"
STABLE_URL = "https://policyengine-simulation-entry-staging-abc-uc.a.run.app"
CANDIDATE_URL = "https://s-42---policyengine-simulation-entry-staging-abc-uc.a.run.app"
DIGEST_IMAGE = (
    "us-central1-docker.pkg.dev/policyengine-api/policyengine-simulation-entry/"
    "policyengine-simulation-entry@sha256:" + "a" * 64
)


FAKE_GCLOUD = """\
case "$1 $2 $3" in
  "run services describe") printf '%s' "$FAKE_SERVICE_JSON" ;;
  "run revisions describe") printf '%s' "$FAKE_REVISION_JSON" ;;
  *) exit 2 ;;
esac
exit "${FAKE_GCLOUD_EXIT:-0}"
"""


def _service(traffic: list[dict], url: str | None = STABLE_URL) -> str:
    status: dict = {"traffic": traffic}
    if url is not None:
        status["url"] = url
    return json.dumps({"status": status})


def _run_cloud_run_script(
    fakes: FakeCommands,
    tmp_path: Path,
    script: str,
    service_json: str,
    revision_json: str = "{}",
    **env: str,
):
    output = tmp_path / "github-output"
    fakes.add("gcloud", FAKE_GCLOUD)
    result = run_script(
        script,
        env=fakes.env(
            **{
                "ENTRYPOINT_SERVICE": SERVICE,
                "PROJECT_ID": PROJECT,
                "REGION": REGION,
                "TAG": "s-42",
                "GITHUB_OUTPUT": str(output),
                "FAKE_SERVICE_JSON": service_json,
                "FAKE_REVISION_JSON": revision_json,
                **env,
            }
        ),
    )
    return result, _output(output)


def _describe_service() -> list[str]:
    return [
        "gcloud",
        "run",
        "services",
        "describe",
        SERVICE,
        "--project",
        PROJECT,
        "--region",
        REGION,
        "--format=json",
    ]


def test_deploy_workflow_runs_the_cloud_run_state_scripts():
    workflow = REUSABLE_DEPLOY_WORKFLOW.read_text(encoding="utf-8")

    previous = _step(workflow, "Capture stable service state before deployment")
    assert "id: previous" in previous
    assert "ENTRYPOINT_SERVICE: ${{ inputs.entrypoint_service }}" in previous
    assert f"run: .github/scripts/{STABLE_STATE}\n" in previous

    candidate = _step(workflow, "Resolve exact candidate metadata")
    assert "id: candidate" in candidate
    assert "ENTRYPOINT_SERVICE: ${{ inputs.entrypoint_service }}" in candidate
    assert "TAG: ${{ steps.metadata.outputs.tag }}" in candidate
    assert f"run: .github/scripts/{CANDIDATE}\n" in candidate

    # The job still reads every output name the scripts write.
    for output in (
        "image: ${{ steps.candidate.outputs.image }}",
        "revision: ${{ steps.candidate.outputs.revision }}",
        "candidate_url: ${{ steps.candidate.outputs.url }}",
        "stable_url: ${{ steps.previous.outputs.stable_url }}",
        "previous_revision: ${{ steps.previous.outputs.revision }}",
    ):
        assert output in workflow


def test_stable_state_records_the_url_and_the_fully_serving_revision(fakes, tmp_path):
    service_json = _service(
        [
            {"revisionName": "entry-00001-old", "percent": 100},
            {"revisionName": "entry-00002-new", "tag": "s-41"},
        ]
    )

    result, output = _run_cloud_run_script(fakes, tmp_path, STABLE_STATE, service_json)

    assert result.returncode == 0, result.stderr
    assert output == f"stable_url={STABLE_URL}\nrevision=entry-00001-old\n"
    assert fakes.calls() == [_describe_service()]


@pytest.mark.parametrize(
    "traffic",
    [
        [
            {"revisionName": "entry-00001-one", "percent": 50},
            {"revisionName": "entry-00002-two", "percent": 50},
        ],
        [
            {"revisionName": "entry-00001-one", "percent": 100},
            {"revisionName": "entry-00002-two", "percent": 100},
        ],
        [],
    ],
    ids=["split", "two-at-100", "no-traffic"],
)
def test_stable_state_requires_exactly_one_revision_at_100_percent(
    fakes, tmp_path, traffic
):
    result, output = _run_cloud_run_script(
        fakes, tmp_path, STABLE_STATE, _service(traffic)
    )

    assert result.returncode != 0
    assert "service must have exactly one revision at 100 percent" in result.stderr
    assert output == ""


def test_stable_state_requires_a_service_url(fakes, tmp_path):
    service_json = _service(
        [{"revisionName": "entry-00001-old", "percent": 100}], url=None
    )

    result, output = _run_cloud_run_script(fakes, tmp_path, STABLE_STATE, service_json)

    assert result.returncode != 0
    assert output == ""


def test_stable_state_fails_when_gcloud_fails(fakes, tmp_path):
    result, output = _run_cloud_run_script(
        fakes, tmp_path, STABLE_STATE, "{}", FAKE_GCLOUD_EXIT="1"
    )

    assert result.returncode != 0
    assert output == ""


CANDIDATE_TRAFFIC = [
    {"revisionName": "entry-00001-old", "percent": 100},
    {"revisionName": "entry-00002-new", "tag": "s-42", "url": CANDIDATE_URL},
]


@pytest.mark.parametrize(
    "revision_json",
    [
        {"status": {"imageDigest": DIGEST_IMAGE}},
        {"spec": {"containers": [{"image": DIGEST_IMAGE}]}},
    ],
    ids=["status-digest", "spec-image"],
)
def test_candidate_resolves_the_tagged_revision_and_its_digest(
    fakes, tmp_path, revision_json
):
    result, output = _run_cloud_run_script(
        fakes,
        tmp_path,
        CANDIDATE,
        _service(CANDIDATE_TRAFFIC),
        json.dumps(revision_json),
    )

    assert result.returncode == 0, result.stderr
    assert output == (
        f"url={CANDIDATE_URL}\nrevision=entry-00002-new\nimage={DIGEST_IMAGE}\n"
    )
    assert fakes.calls() == [
        _describe_service(),
        [
            "gcloud",
            "run",
            "revisions",
            "describe",
            "entry-00002-new",
            "--project",
            PROJECT,
            "--region",
            REGION,
            "--format=json",
        ],
    ]


def test_candidate_rejects_an_image_not_pinned_by_digest(fakes, tmp_path):
    revision_json = {
        "spec": {"containers": [{"image": "example/policyengine-simulation-entry:a"}]}
    }

    result, output = _run_cloud_run_script(
        fakes,
        tmp_path,
        CANDIDATE,
        _service(CANDIDATE_TRAFFIC),
        json.dumps(revision_json),
    )

    assert result.returncode != 0
    assert output == ""


def test_candidate_fails_when_the_tag_is_not_deployed(fakes, tmp_path):
    result, output = _run_cloud_run_script(
        fakes, tmp_path, CANDIDATE, _service(CANDIDATE_TRAFFIC), TAG="s-43"
    )

    assert result.returncode != 0
    assert output == ""
    assert fakes.calls() == [_describe_service()]
