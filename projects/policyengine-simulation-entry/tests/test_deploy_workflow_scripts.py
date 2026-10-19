"""Tests for the scripts the simulation deployment workflows run.

Each test runs a script with fake gcloud or uv commands first on PATH (jq is
real) and asserts on the exact calls, $GITHUB_OUTPUT and exit status, so the
production steps keep behaving as the inline shell they replaced.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

import pytest
from workflow_script_support import (
    WORKFLOWS_DIR,
    FakeCommands,
    run_script,
)

if TYPE_CHECKING:
    from pathlib import Path

REUSABLE_DEPLOY_WORKFLOW = WORKFLOWS_DIR / "simulation-deploy.reusable.yml"


@pytest.fixture
def fakes(tmp_path: Path) -> FakeCommands:
    return FakeCommands.create(tmp_path)


def _step(workflow: str, name: str) -> str:
    """Return one workflow step's text, from its ``- name:`` to the next step."""
    match = re.search(
        rf"^(?P<indent> +)- name: {re.escape(name)}\n"
        r"(?P<body>.*?)(?=^(?P=indent)- |^  \S|\Z)",
        workflow,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert match is not None, name
    return match.group("body")


def _output(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def test_every_job_checks_out_the_repository_before_running_its_scripts():
    import yaml

    jobs = yaml.safe_load(REUSABLE_DEPLOY_WORKFLOW.read_text(encoding="utf-8"))["jobs"]
    for job_name, job in jobs.items():
        checked_out = False
        for step in job.get("steps", []):
            if str(step.get("uses", "")).startswith("actions/checkout@"):
                checked_out = True
            if ".github/scripts/" in step.get("run", ""):
                assert checked_out, f"{job_name}: {step.get('name')}"


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


# --- deploy_stage12_v2 and stage12_v2_ready -------------------------------

V2_APPLICATION_NAME = "stage12-v2-application-name.sh"
VALIDATE_V2_WORKERS = "stage12-validate-v2-workers.sh"
CACHE_MANIFEST_SHA256 = "a" * 64
STAGE12_READY = "stage12-deployment-ready.sh"
V2_APPLICATION_CODE = (
    "from policyengine_simulation_contract.stage12_manifest import "
    "v2_application_name; from policyengine_simulation_executor.stage12_bundle "
    "import load_stage12_bundle; print(v2_application_name("
    "load_stage12_bundle().bundle.policyengine_version))"
)


def test_deploy_workflow_runs_the_stage12_worker_scripts():
    workflow = REUSABLE_DEPLOY_WORKFLOW.read_text(encoding="utf-8")

    application = _step(workflow, "Select the bundle-derived v2 application name")
    assert "id: application" in application
    assert "working-directory: projects/policyengine-simulation-executor" in (
        application
    )
    assert f"run: ../../.github/scripts/{V2_APPLICATION_NAME}\n" in application

    validation = _step(
        workflow, "Validate installed bundles, secrets, datasets, and calculations"
    )
    assert "id: validation" in validation
    assert "working-directory: projects/policyengine-simulation-executor" in (
        validation
    )
    assert (
        f"run: ../../.github/scripts/{VALIDATE_V2_WORKERS} "
        '"${{ inputs.modal_environment }}" '
        '"${{ steps.stage12_cache.outputs.manifest_digest }}"\n' in validation
    )

    assert "application_name: ${{ steps.application.outputs.name }}" in workflow
    assert "validation_file: ${{ steps.validation.outputs.file }}" in workflow
    assert "path: ${{ steps.validation.outputs.file }}" in workflow


def test_v2_application_name_comes_from_the_packaged_bundle(fakes, tmp_path):
    output = tmp_path / "github-output"
    fakes.add("uv", "echo policyengine-simulation-v2-py4-1-0\n")

    result = run_script(V2_APPLICATION_NAME, env=fakes.env(GITHUB_OUTPUT=str(output)))

    assert result.returncode == 0, result.stderr
    assert _output(output) == "name=policyengine-simulation-v2-py4-1-0\n"
    assert fakes.calls() == [["uv", "run", "python", "-c", V2_APPLICATION_CODE]]


def test_v2_application_name_fails_without_writing_when_the_bundle_fails(
    fakes, tmp_path
):
    output = tmp_path / "github-output"
    fakes.add("uv", "exit 1\n")

    result = run_script(V2_APPLICATION_NAME, env=fakes.env(GITHUB_OUTPUT=str(output)))

    assert result.returncode == 1
    assert _output(output) == ""


def _validate_v2_workers(fakes: FakeCommands, tmp_path: Path, *args: str):
    output = tmp_path / "github-output"
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir()
    result = run_script(
        VALIDATE_V2_WORKERS,
        *args,
        env=fakes.env(GITHUB_OUTPUT=str(output), RUNNER_TEMP=str(runner_temp)),
    )
    return result, _output(output), runner_temp / "stage12-worker-validation.json"


def test_validate_v2_workers_records_the_validation_file(fakes, tmp_path):
    fakes.add("uv")

    result, output, validation_file = _validate_v2_workers(
        fakes, tmp_path, "main", CACHE_MANIFEST_SHA256
    )

    assert result.returncode == 0, result.stderr
    assert output == f"file={validation_file}\n"
    assert fakes.calls() == [
        [
            "uv",
            "run",
            "python",
            "-m",
            "src.modal.utils.validate_v2_worker_release",
            "--environment",
            "main",
            "--cache-manifest-sha256",
            CACHE_MANIFEST_SHA256,
            "--output",
            str(validation_file),
        ]
    ]


def test_validate_v2_workers_fails_without_writing_when_validation_fails(
    fakes, tmp_path
):
    fakes.add("uv", "exit 1\n")

    result, output, _ = _validate_v2_workers(
        fakes, tmp_path, "main", CACHE_MANIFEST_SHA256
    )

    assert result.returncode == 1
    assert output == ""


def test_validate_v2_workers_requires_the_modal_environment(fakes, tmp_path):
    fakes.add("uv")

    result, output, _ = _validate_v2_workers(fakes, tmp_path)

    assert result.returncode != 0
    assert "Modal environment required" in result.stderr
    assert output == ""
    assert fakes.calls() == []


def test_validate_v2_workers_requires_the_cache_manifest_digest(fakes, tmp_path):
    fakes.add("uv")

    result, output, _ = _validate_v2_workers(fakes, tmp_path, "main")

    assert result.returncode != 0
    assert "Stage 12 cache manifest digest required" in result.stderr
    assert output == ""
    assert fakes.calls() == []


def test_deploy_workflow_checks_stage12_results_with_the_tested_script():
    workflow = REUSABLE_DEPLOY_WORKFLOW.read_text(encoding="utf-8")
    step = _step(workflow, "Check Stage 12 deployment result")

    for declaration in (
        "REQUESTED: ${{ inputs.deploy_stage12_v2 }}",
        "CONFIGURATION_RESULT: ${{ needs.configure_stage12_v2.result }}",
        "DEPLOYMENT_RESULT: ${{ needs.deploy_stage12_v2.result }}",
        "PUBLICATION_RESULT: ${{ needs.publish_stage12_v2_manifest.result }}",
        f"run: .github/scripts/{STAGE12_READY}\n",
    ):
        assert declaration in step


@pytest.mark.parametrize(
    ("requested", "configuration", "deployment", "publication", "passes"),
    [
        ("true", "success", "success", "success", True),
        ("true", "success", "success", "failure", False),
        ("true", "success", "skipped", "skipped", False),
        ("true", "skipped", "skipped", "skipped", False),
        ("false", "skipped", "skipped", "skipped", True),
        ("false", "success", "skipped", "skipped", False),
        ("false", "skipped", "skipped", "failure", False),
    ],
)
def test_stage12_ready_requires_all_or_none_of_the_v2_jobs(
    fakes, requested, configuration, deployment, publication, passes
):
    result = run_script(
        STAGE12_READY,
        env=fakes.env(
            REQUESTED=requested,
            CONFIGURATION_RESULT=configuration,
            DEPLOYMENT_RESULT=deployment,
            PUBLICATION_RESULT=publication,
        ),
    )

    assert result.returncode == (0 if passes else 1)


# --- deployment_ready -------------------------------------------------------

DEPLOYMENT_READY = "simulation-deployment-ready.sh"


def test_deploy_workflow_checks_deployment_results_with_the_tested_script():
    workflow = REUSABLE_DEPLOY_WORKFLOW.read_text(encoding="utf-8")
    step = _step(workflow, "Check deployment result")

    for declaration in (
        "PROMOTION_REQUESTED: ${{ inputs.promote_entrypoint }}",
        "ENTRYPOINT_RESULT: ${{ needs.deploy_entrypoint.result }}",
        "INTEGRATION_RESULT: ${{ needs.integration.result }}",
        "AUTHENTICATED_TEST_RESULT: ${{ needs.authenticated_test.result }}",
        "PROMOTION_RESULT: ${{ needs.promote_entrypoint.result }}",
        "EXISTING_STACK_REQUESTED: ${{ inputs.deploy_existing_stack }}",
        "ROUTING_RESULT: ${{ needs.update_routing.result }}",
        "FINALIZE_RESULT: ${{ needs.finalize_routing.result }}",
        "FINALIZE_OUTCOME: ${{ needs.finalize_routing.outputs.outcome }}",
        f"run: .github/scripts/{DEPLOYMENT_READY}\n",
    ):
        assert declaration in step


@pytest.mark.parametrize(
    ("promotion_requested", "results", "passes"),
    [
        ("true", ("success", "success", "success", "success"), True),
        ("false", ("success", "success", "success", "skipped"), True),
        ("true", ("success", "success", "success", "skipped"), False),
        ("false", ("success", "success", "success", "success"), False),
        ("true", ("success", "success", "skipped", "success"), False),
        ("false", ("success", "skipped", "success", "skipped"), False),
        ("false", ("failure", "success", "success", "skipped"), False),
    ],
)
def test_deployment_ready_rejects_unexpected_skipped_phases(
    fakes, promotion_requested, results, passes
):
    entrypoint, integration, authenticated_test, promotion = results

    result = run_script(
        DEPLOYMENT_READY,
        env=fakes.env(
            PROMOTION_REQUESTED=promotion_requested,
            ENTRYPOINT_RESULT=entrypoint,
            INTEGRATION_RESULT=integration,
            AUTHENTICATED_TEST_RESULT=authenticated_test,
            PROMOTION_RESULT=promotion,
            EXISTING_STACK_REQUESTED="false",
            ROUTING_RESULT="skipped",
            FINALIZE_RESULT="skipped",
            FINALIZE_OUTCOME="",
        ),
    )

    assert result.returncode == (0 if passes else 1)


@pytest.mark.parametrize(
    ("routing", "finalize", "outcome", "passes"),
    [
        ("success", "success", "completed", True),
        ("success", "success", "restored", False),
        ("success", "failure", "completed", False),
        ("success", "skipped", "", False),
        ("skipped", "success", "completed", False),
        ("failure", "success", "completed", False),
    ],
)
def test_deployment_ready_requires_a_completed_routing_publication(
    fakes, routing, finalize, outcome, passes
):
    result = run_script(
        DEPLOYMENT_READY,
        env=fakes.env(
            PROMOTION_REQUESTED="true",
            ENTRYPOINT_RESULT="success",
            INTEGRATION_RESULT="success",
            AUTHENTICATED_TEST_RESULT="success",
            PROMOTION_RESULT="success",
            EXISTING_STACK_REQUESTED="true",
            ROUTING_RESULT=routing,
            FINALIZE_RESULT=finalize,
            FINALIZE_OUTCOME=outcome,
        ),
    )

    assert result.returncode == (0 if passes else 1)
