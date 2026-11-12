"""The staging economy check, run on every staging deploy.

``simulation-staging-economy-check.sh`` creates a small economy report,
current law against current law for Wales in 2026, through the deployed
candidate's ``POST /reports``, then follows its run through
``GET /reports/{report_id}`` and requires it to succeed. It runs after the
v2 manifest's default has moved to the release's worker, so the report runs
on the release's own coordinator. Once a .py version's first check has
succeeded, later checks on it reuse those simulation runs, and check the
coordinator's use of their stored output.
"""

from __future__ import annotations

import json

import pytest
from test_deployment_assets_support import (
    REUSABLE_DEPLOY_WORKFLOW,
    evaluate_github_expression,
    routing_context,
    workflow_jobs,
)
from workflow_script_support import FakeCommands, run_script

CHECK_SCRIPT = "simulation-staging-economy-check.sh"
READY_SCRIPT = "simulation-staging-economy-ready.sh"
CHECK_JOB = "staging_economy_check"
BASE_URL = "https://policyengine-simulation-entry-staging-s-123.a.run.app"
REPORT_ID = "00000000-0000-4000-8000-0000000000e1"
REPORT_RUN_ID = "00000000-0000-4000-8000-0000000000e2"
OLDER_RUN_ID = "00000000-0000-4000-8000-0000000000e3"
ACCESS_TOKEN = "test-access-token"

# The fake curl answers the token request, the submission and each poll in
# turn, and keeps each request body it was sent on standard input.
FAKE_CURL = r"""
output=""
url=""
method="GET"
data=""
while (($#)); do
  case "$1" in
    --output) output="$2"; shift 2 ;;
    --request) method="$2"; shift 2 ;;
    --data) data="$2"; shift 2 ;;
    --header | --write-out) shift 2 ;;
    *) url="$1"; shift ;;
  esac
done
calls="$(($(cat "${FAKE_STATE}/calls" 2>/dev/null || echo 0) + 1))"
echo "${calls}" > "${FAKE_STATE}/calls"
if [[ "${data}" == "@-" ]]; then
  cat > "${FAKE_STATE}/body-${calls}.json"
fi
case "${method} ${url}" in
  "POST https://issuer.example/oauth/token")
    printf '{"access_token": "%s"}' "${FAKE_ACCESS_TOKEN}" > "${output}"
    printf '200'
    ;;
  "POST ${FAKE_BASE_URL}/reports")
    printf '%s' "${FAKE_SUBMISSION_BODY}" > "${output}"
    printf '%s' "${FAKE_SUBMISSION_STATUS}"
    ;;
  "GET ${FAKE_BASE_URL}/reports/"*)
    echo "${url}" > "${FAKE_STATE}/read-url"
    polls="$(($(cat "${FAKE_STATE}/polls" 2>/dev/null || echo 0) + 1))"
    echo "${polls}" > "${FAKE_STATE}/polls"
    answer="$(sed -n "${polls}p" "${FAKE_STATE}/answers")"
    printf '%s' "${answer#* }" > "${output}"
    printf '%s' "${answer%% *}"
    ;;
  *)
    printf 'unexpected request: %s %s\n' "${method}" "${url}" >&2
    exit 7
    ;;
esac
"""
CREATED = {
    "report_id": REPORT_ID,
    "report_run_id": REPORT_RUN_ID,
    "status": "pending",
    "baseline_simulation_id": "00000000-0000-4000-8000-0000000000f1",
    "baseline_simulation_run_id": "00000000-0000-4000-8000-0000000000f2",
    "baseline_reused": True,
    "reform_simulation_id": "00000000-0000-4000-8000-0000000000f3",
    "reform_simulation_run_id": "00000000-0000-4000-8000-0000000000f4",
    "reform_reused": False,
}


def _report(status: str, *, run_id: str = REPORT_RUN_ID, **run_fields) -> str:
    return json.dumps(
        {
            "report_id": REPORT_ID,
            "country": "uk",
            "baseline_simulation_id": CREATED["baseline_simulation_id"],
            "reform_simulation_id": CREATED["reform_simulation_id"],
            "run": {
                "run_id": run_id,
                "status": status,
                "country_package_version": "2.174.1",
                "policyengine_version": "6.17.1",
                "error_message": None,
                "created_at": "2026-11-12T15:00:00Z",
                "started_at": None,
                "completed_at": None,
                "baseline_simulation_run_id": CREATED["baseline_simulation_run_id"],
                "reform_simulation_run_id": CREATED["reform_simulation_run_id"],
                **run_fields,
            },
        }
    )


@pytest.fixture
def fakes(tmp_path) -> FakeCommands:
    commands = FakeCommands.create(tmp_path)
    commands.add("curl", FAKE_CURL)
    commands.add("sleep")
    return commands


def _run(fakes, tmp_path, answers: list[str], **overrides):
    """Run the check; ``answers`` are the polls' "<HTTP status> <body>" in order."""
    state = tmp_path / "state"
    state.mkdir()
    (state / "answers").write_text("\n".join(answers) + "\n", encoding="utf-8")
    environment = {
        "SIMULATION_TEST_AUTH_ISSUER": "https://issuer.example/",
        "SIMULATION_TEST_AUTH_AUDIENCE": "simulation-entry",
        "SIMULATION_TEST_AUTH_CLIENT_ID": "staging-test-client",
        "SIMULATION_TEST_AUTH_CLIENT_SECRET": "staging-test-secret",
        "FAKE_STATE": str(state),
        "FAKE_BASE_URL": BASE_URL,
        "FAKE_ACCESS_TOKEN": ACCESS_TOKEN,
        "FAKE_SUBMISSION_STATUS": "201",
        "FAKE_SUBMISSION_BODY": json.dumps(CREATED),
        **overrides,
    }
    result = run_script(CHECK_SCRIPT, BASE_URL + "/", env=fakes.env(**environment))
    return result, state


def test_the_check_creates_a_small_report_and_waits_for_its_run_to_succeed(
    fakes, tmp_path
):
    result, state = _run(
        fakes,
        tmp_path,
        [
            f"200 {_report('pending')}",
            f"200 {_report('running')}",
            f"200 {_report('succeeded', completed_at='2026-11-12T15:05:00Z')}",
        ],
    )

    assert result.returncode == 0, result.stderr
    assert REPORT_ID in result.stdout
    assert "reused" in result.stdout
    token_request = json.loads((state / "body-1.json").read_text(encoding="utf-8"))
    assert token_request["grant_type"] == "client_credentials"
    submission = json.loads((state / "body-2.json").read_text(encoding="utf-8"))
    assert submission == {
        "country": "uk",
        "report_type": "economy",
        "label": "Staging economy check",
        "year": 2026,
        "region_code": "country/wales",
        "baseline": {"policy_id": None, "dynamic_id": None},
        "reform": {"policy_id": None, "dynamic_id": None},
        "include_cliffs": False,
    }
    assert (state / "read-url").read_text(encoding="utf-8").strip() == (
        f"{BASE_URL}/reports/{REPORT_ID}"
    )
    assert [call[1:] for call in fakes.calls("sleep")] == [["15"], ["15"]]


def test_no_secret_reaches_a_command_line(fakes, tmp_path):
    """The token is sent from a file, and the client secret on standard input."""
    result, _ = _run(fakes, tmp_path, [f"200 {_report('succeeded')}"])

    assert result.returncode == 0, result.stderr
    arguments = [argument for call in fakes.calls("curl") for argument in call]
    assert not any(ACCESS_TOKEN in argument for argument in arguments)
    assert not any("staging-test-secret" in argument for argument in arguments)


def test_a_failed_run_fails_the_check_with_its_error_message(fakes, tmp_path):
    failed = _report(
        "failed", error_message="simulation_failed: the reform ran out of memory"
    )

    result, _ = _run(fakes, tmp_path, [f"200 {failed}"])

    assert result.returncode == 1
    assert "simulation_failed: the reform ran out of memory" in result.stderr
    assert fakes.calls("sleep") == []


def test_a_newest_run_other_than_the_created_one_fails_the_check(fakes, tmp_path):
    result, _ = _run(
        fakes, tmp_path, [f"200 {_report('succeeded', run_id=OLDER_RUN_ID)}"]
    )

    assert result.returncode == 1
    assert OLDER_RUN_ID in result.stderr


@pytest.mark.parametrize(
    ("status", "body"),
    [
        ("400", {"code": "unknown_region", "detail": "no region country/wales"}),
        ("503", {"code": "modal_call_refused", "detail": "x", "report_id": REPORT_ID}),
    ],
)
def test_a_refused_submission_fails_the_check_with_its_answer(
    fakes, tmp_path, status, body
):
    result, state = _run(
        fakes,
        tmp_path,
        [],
        FAKE_SUBMISSION_STATUS=status,
        FAKE_SUBMISSION_BODY=json.dumps(body),
    )

    assert result.returncode == 1
    assert status in result.stderr
    assert body["code"] in result.stderr
    assert not (state / "polls").exists()


def test_an_unavailable_read_is_asked_again(fakes, tmp_path):
    unavailable = json.dumps({"code": "database_unavailable", "detail": "x"})

    result, _ = _run(
        fakes, tmp_path, [f"503 {unavailable}", f"200 {_report('succeeded')}"]
    )

    assert result.returncode == 0, result.stderr


def test_any_other_read_answer_fails_the_check(fakes, tmp_path):
    result, _ = _run(fakes, tmp_path, ['404 {"code": "unknown_report"}'])

    assert result.returncode == 1
    assert "404" in result.stderr


def test_a_run_that_does_not_finish_in_time_fails_the_check(fakes, tmp_path):
    result, _ = _run(
        fakes,
        tmp_path,
        [f"200 {_report('pending')}"],
        ECONOMY_CHECK_DEADLINE_SECONDS="0",
    )

    assert result.returncode == 1
    assert "did not finish within 0 seconds" in result.stderr


def test_the_check_needs_the_test_client_credentials(fakes, tmp_path):
    result, _ = _run(fakes, tmp_path, [], SIMULATION_TEST_AUTH_CLIENT_ID="")

    assert result.returncode == 1
    assert "SIMULATION_TEST_AUTH_CLIENT_ID" in result.stderr
    assert fakes.calls("curl") == []


# --- The workflow job --------------------------------------------------------


def _jobs() -> dict:
    return workflow_jobs(REUSABLE_DEPLOY_WORKFLOW)


def _context(**overrides):
    context = routing_context()
    context["inputs"]["deployment_environment"] = "staging"
    context["inputs"]["deploy_stage12_v2"] = True
    context["needs"]["deploy_entrypoint"] = {"result": "success"}
    context["needs"]["publish_stage12_v2_default"] = {"result": "success"}
    for path, value in overrides.items():
        target = context
        *parents, last = path.split("__")
        for part in parents:
            target = target[part]
        target[last] = value
    return context


def test_the_check_job_creates_the_report_through_the_candidate():
    job = _jobs()[CHECK_JOB]
    (step,) = [step for step in job["steps"] if "run" in step]

    assert set(job["needs"]) == {
        "deploy_entrypoint",
        "authenticated_test",
        "publish_stage12_v2_default",
    }
    assert job["environment"] == "${{ inputs.release_environment }}"
    assert step["run"] == (
        f".github/scripts/{CHECK_SCRIPT} "
        '"${{ needs.deploy_entrypoint.outputs.candidate_url }}"'
    )
    assert step["env"] == {
        "SIMULATION_TEST_AUTH_ISSUER": (
            "${{ secrets.SIMULATION_ENTRYPOINT_AUTH_ISSUER }}"
        ),
        "SIMULATION_TEST_AUTH_AUDIENCE": (
            "${{ secrets.SIMULATION_ENTRYPOINT_AUTH_AUDIENCE }}"
        ),
        "SIMULATION_TEST_AUTH_CLIENT_ID": (
            "${{ secrets.SIMULATION_ENTRYPOINT_TEST_AUTH_CLIENT_ID }}"
        ),
        "SIMULATION_TEST_AUTH_CLIENT_SECRET": (
            "${{ secrets.SIMULATION_ENTRYPOINT_TEST_AUTH_CLIENT_SECRET }}"
        ),
    }


@pytest.mark.parametrize(
    ("overrides", "runs"),
    [
        ({}, True),
        ({"inputs__deployment_environment": "production"}, False),
        ({"needs__deploy_entrypoint__result": "failure"}, False),
        ({"needs__authenticated_test__result": "failure"}, False),
        ({"needs__publish_stage12_v2_default__result": "failure"}, False),
        ({"needs__publish_stage12_v2_default__result": "skipped"}, False),
        (
            {
                "inputs__deploy_stage12_v2": False,
                "needs__publish_stage12_v2_default__result": "skipped",
            },
            True,
        ),
        ({"cancelled": True}, False),
    ],
    ids=[
        "staging",
        "production",
        "entrypoint-failed",
        "authenticated-tests-failed",
        "default-move-failed",
        "default-move-skipped",
        "no-stage12",
        "cancelled",
    ],
)
def test_the_check_runs_on_every_staging_deploy_after_the_default_moves(
    overrides, runs
):
    job = _jobs()[CHECK_JOB]

    assert (
        bool(evaluate_github_expression(job["if"], _context(**overrides), job_if=True))
        is runs
    )


def test_deployment_ready_requires_the_staging_economy_check():
    ready = _jobs()["deployment_ready"]
    (step,) = [
        step
        for step in ready["steps"]
        if step.get("name") == "Check the staging economy check result"
    ]

    assert CHECK_JOB in ready["needs"]
    assert step["run"] == f".github/scripts/{READY_SCRIPT}"
    assert step["env"] == {
        "REQUESTED": "${{ inputs.deployment_environment == 'staging' }}",
        "CHECK_RESULT": "${{ needs.staging_economy_check.result }}",
    }


@pytest.mark.parametrize(
    ("requested", "check_result", "passes"),
    [
        ("true", "success", True),
        ("true", "failure", False),
        ("true", "skipped", False),
        ("true", "cancelled", False),
        ("false", "skipped", True),
        ("false", "success", False),
    ],
)
def test_the_ready_check_requires_the_check_exactly_in_staging(
    fakes, requested, check_result, passes
):
    result = run_script(
        READY_SCRIPT,
        env=fakes.env(REQUESTED=requested, CHECK_RESULT=check_result),
    )

    assert result.returncode == (0 if passes else 1)
