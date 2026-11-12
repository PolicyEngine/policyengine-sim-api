"""The staging preemption check, run on every staging deploy.

``simulation-staging-preemption-check.sh`` starts a calculation through the
deployed candidate, a household calculation or the staging economy check's
report, and once the household function or coordinator has claimed it,
stops the Modal container running its call with ``modal container stop``.
That sends the container SIGINT and gives its input to another container
under the same call ID, as a pre-emption does. The check passes once the
calculation succeeds and Modal's call graph shows its call finishing in
another container. A calculation that finishes before its container is
stopped proves nothing and is submitted again, up to three times.

The fakes stand in for curl (the token issuer and the service) and uv (the
call-input helper and the Modal CLI). Each record's reads, and each call's
inputs, follow one list of answers until the record's container is stopped
and another after.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest
from test_deployment_assets_support import (
    REUSABLE_DEPLOY_WORKFLOW,
    evaluate_github_expression,
    routing_context,
    workflow_jobs,
)
from workflow_script_support import FakeCommands, run_script

if TYPE_CHECKING:
    from pathlib import Path

CHECK_SCRIPT = "simulation-staging-preemption-check.sh"
READY_SCRIPT = "simulation-staging-preemption-ready.sh"
CHECK_JOB = "staging_preemption_check"
BASE_URL = "https://policyengine-simulation-entry-staging-s-123.a.run.app"
HOUSEHOLD_ID = "00000000-0000-4000-8000-0000000000bb"
ACCESS_TOKEN = "test-access-token"
CLIENT_SECRET = "staging-test-secret"
JOB_IDS = [f"00000000-0000-4000-8000-00000000a00{digit}" for digit in "123"]
REPORT_IDS = [f"00000000-0000-4000-8000-00000000b00{digit}" for digit in "123"]
REPORT_RUN_IDS = [f"00000000-0000-4000-8000-00000000c00{digit}" for digit in "123"]
CALL_IDS = [f"fc-01CALL{digit}" for digit in "123"]
CONTAINERS = [f"ta-01CONTAINER{digit}" for digit in "123"]
RERUN_CONTAINER = "ta-01RERUN"
REPORT_REQUEST = {
    "country": "uk",
    "report_type": "economy",
    "label": "Staging preemption check",
    "year": 2026,
    "region_code": "country/wales",
    "baseline": {"policy_id": None, "dynamic_id": None},
    "reform": {"policy_id": None, "dynamic_id": None},
    "include_cliffs": False,
}

# The fake curl answers the token request, each submission in turn, and each
# record's reads from the spec, keeping each request body it was sent.
FAKE_CURL = r"""
spec="${FAKE_STATE}/spec.json"
output=""
url=""
method="GET"
data=""
while (($#)); do
  case "$1" in
    --output) output="$2"; shift 2 ;;
    --request) method="$2"; shift 2 ;;
    --data) data="$2"; shift 2 ;;
    --header | --write-out | --max-time) shift 2 ;;
    *) url="$1"; shift ;;
  esac
done
next() {
  local counter="${FAKE_STATE}/$1"
  local count="$(($(cat "${counter}" 2>/dev/null || echo 0) + 1))"
  echo "${count}" > "${counter}"
  echo "${count}"
}
calls="$(next calls)"
if [[ "${data}" == "@-" ]]; then
  cat > "${FAKE_STATE}/body-${calls}.json"
fi
case "${method} ${url}" in
  "POST https://issuer.example/oauth/token")
    printf '{"access_token": "%s"}' "${FAKE_ACCESS_TOKEN}" > "${output}"
    printf '200'
    ;;
  "POST ${FAKE_BASE_URL}/household-calculations" \
    | "POST ${FAKE_BASE_URL}/reports")
    submission="$(next submissions)"
    cp "${FAKE_STATE}/body-${calls}.json" "${FAKE_STATE}/submission-${submission}.json"
    index="$(($(cat "${FAKE_STATE}/submissions") - 1))"
    jq -r ".submissions[${index}].body" "${spec}" > "${output}"
    jq -r ".submissions[${index}].status" "${spec}" | tr -d '\n'
    ;;
  "GET ${FAKE_BASE_URL}/household-calculations/"* \
    | "GET ${FAKE_BASE_URL}/reports/"*)
    echo "${url}" >> "${FAKE_STATE}/read-urls"
    record_id="${url##*/}"
    container="$(jq -r --arg id "${record_id}" '.records[$id].container' "${spec}")"
    phase="before"
    if [[ -e "${FAKE_STATE}/stopped-${container}" ]]; then phase="after"; fi
    count="$(next "reads-${record_id}-${phase}")"
    answer="$(jq -r --arg id "${record_id}" --arg phase "${phase}" \
      --argjson count "${count}" \
      '.records[$id][$phase] | .[([$count, length] | min) - 1]' "${spec}")"
    if [[ "${answer}" == "TRANSPORT" ]]; then
      # curl's exit status when the request times out.
      exit 28
    fi
    printf '%s' "${answer#* }" > "${output}"
    printf '%s' "${answer%% *}"
    ;;
  *)
    printf 'unexpected request: %s %s\n' "${method}" "${url}" >&2
    exit 7
    ;;
esac
"""

# The fake uv runs the call-input helper and the Modal CLI's container
# commands from the spec.
FAKE_UV = r"""
spec="${FAKE_STATE}/spec.json"
last="${*: -1}"
case "$*" in
  "run --frozen python -m src.modal.utils.describe_call_inputs "*)
    container="$(jq -r --arg id "${last}" '.calls[$id].container // empty' "${spec}")"
    phase="before"
    if [[ -n "${container}" && -e "${FAKE_STATE}/stopped-${container}" ]]; then
      phase="after"
    fi
    counter="${FAKE_STATE}/inputs-${last}-${phase}"
    count="$(($(cat "${counter}" 2>/dev/null || echo 0) + 1))"
    echo "${count}" > "${counter}"
    answer="$(jq -c --arg id "${last}" --arg phase "${phase}" \
      --argjson count "${count}" \
      '(.calls[$id][$phase] // [[]]) | .[([$count, length] | min) - 1]' "${spec}")"
    if [[ "${answer}" == '"ERROR"' ]]; then
      printf "Could not read Modal call %s's call graph: NotFoundError\n" "${last}" >&2
      exit 1
    fi
    printf '%s\n' "${answer}"
    ;;
  "run --frozen modal container list --env "*" --json")
    jq '[.listed[] | {"Container ID": ., "App ID": "ap-01V2",
      "App Name": "policyengine-v2", "Start Time": "2026-11-13T12:00:00+00:00"}]' \
      "${spec}"
    ;;
  "run --frozen modal container stop --yes "*)
    if jq -e --arg id "${last}" '.refused_stops | index($id)' "${spec}" >/dev/null; then
      printf "Container '%s' is already stopped.\n" "${last}" >&2
      exit 1
    fi
    touch "${FAKE_STATE}/stopped-${last}"
    ;;
  *)
    printf 'unexpected uv command: %s\n' "$*" >&2
    exit 9
    ;;
esac
"""


def _household(job_id: str, status: str, call_id: str | None = None, **fields) -> str:
    """Return a household job read: "<HTTP status> <body>"."""
    body = {
        "job_id": job_id,
        "status": status,
        "country": "us",
        "household_id": HOUSEHOLD_ID,
        "policy_id": None,
        "year": 2026,
        "policyengine_version": "6.17.1",
        "country_package_version": "1.400.0",
        "modal_call_id": call_id,
        "result": {"status": "succeeded"} if status == "succeeded" else None,
        "error_message": None,
        "created_at": "2026-11-13T12:00:00Z",
        "started_at": None,
        "completed_at": None,
        **fields,
    }
    return f"200 {json.dumps(body)}"


def _report(attempt: int, status: str, call_id: str | None = None, **run_fields) -> str:
    """Return a report read for the ``attempt``th report: "<HTTP status> <body>"."""
    body = {
        "report_id": REPORT_IDS[attempt],
        "country": "uk",
        "baseline_simulation_id": "00000000-0000-4000-8000-0000000000f1",
        "reform_simulation_id": "00000000-0000-4000-8000-0000000000f3",
        "run": {
            "run_id": REPORT_RUN_IDS[attempt],
            "status": status,
            "country_package_version": "2.174.1",
            "policyengine_version": "6.17.1",
            "error_message": None,
            "created_at": "2026-11-13T12:00:00Z",
            "started_at": None,
            "completed_at": None,
            "baseline_simulation_run_id": "00000000-0000-4000-8000-0000000000f2",
            "reform_simulation_run_id": "00000000-0000-4000-8000-0000000000f4",
            "modal_call_id": call_id,
            **run_fields,
        },
    }
    return f"200 {json.dumps(body)}"


def _inputs(*containers_and_statuses: tuple[str, str]) -> list[dict[str, str]]:
    """Return what the call-input helper prints for inputs on these containers."""
    return [
        {"input_id": f"in-{index}", "container_id": container, "status": status}
        for index, (container, status) in enumerate(containers_and_statuses)
    ]


@dataclass
class Attempt:
    """One submission's answers: before its container is stopped, and after."""

    record_id: str
    submission: dict
    call_id: str
    container: str
    reads_before_stop: list[str]
    reads_after_stop: list[str] = field(default_factory=list)
    inputs_before_stop: list = field(default_factory=list)
    inputs_after_stop: list = field(default_factory=list)
    submission_status: str = "202"


def _household_attempt(attempt: int, *, proves: bool = True) -> Attempt:
    """Return a household attempt whose container is stopped once it's running.

    Without ``proves``, the job finishes before the check sees it running.
    """
    job_id, call_id, container = (
        JOB_IDS[attempt],
        CALL_IDS[attempt],
        CONTAINERS[attempt],
    )
    if not proves:
        return Attempt(
            record_id=job_id,
            submission={"job_id": job_id, "status": "pending"},
            call_id=call_id,
            container=container,
            reads_before_stop=[
                _household(job_id, "pending", call_id),
                _household(job_id, "succeeded", call_id),
            ],
            inputs_before_stop=[_inputs((container, "PENDING"))],
        )
    return Attempt(
        record_id=job_id,
        submission={"job_id": job_id, "status": "pending"},
        call_id=call_id,
        container=container,
        reads_before_stop=[
            _household(job_id, "pending"),
            _household(job_id, "pending", call_id),
            _household(job_id, "running", call_id),
        ],
        reads_after_stop=[
            _household(job_id, "running", call_id),
            _household(job_id, "succeeded", call_id),
        ],
        inputs_before_stop=[_inputs((container, "PENDING"))],
        inputs_after_stop=[
            _inputs((container, "TERMINATED"), (RERUN_CONTAINER, "SUCCESS"))
        ],
    )


def _report_attempt(attempt: int) -> Attempt:
    """Return a report attempt whose coordinator container is stopped once running."""
    call_id, container = CALL_IDS[attempt], CONTAINERS[attempt]
    return Attempt(
        record_id=REPORT_IDS[attempt],
        submission={
            "report_id": REPORT_IDS[attempt],
            "report_run_id": REPORT_RUN_IDS[attempt],
            "status": "pending",
            "baseline_reused": True,
            "reform_reused": True,
        },
        submission_status="201",
        call_id=call_id,
        container=container,
        reads_before_stop=[
            _report(attempt, "pending", call_id),
            _report(attempt, "running", call_id),
        ],
        reads_after_stop=[
            _report(attempt, "running", call_id),
            _report(attempt, "succeeded", call_id),
        ],
        inputs_before_stop=[_inputs((container, "PENDING"))],
        inputs_after_stop=[
            _inputs((container, "TERMINATED"), (RERUN_CONTAINER, "SUCCESS"))
        ],
    )


@dataclass(frozen=True)
class ModalContainers:
    """The containers Modal lists in staging, and those it refuses to stop."""

    listed: tuple[str, ...] = (*CONTAINERS, RERUN_CONTAINER)
    refused_stops: tuple[str, ...] = ()


@pytest.fixture
def fakes(tmp_path) -> FakeCommands:
    commands = FakeCommands.create(tmp_path)
    commands.add("curl", FAKE_CURL)
    commands.add("uv", FAKE_UV)
    commands.add("sleep")
    return commands


def _run(
    fakes: FakeCommands,
    tmp_path: Path,
    kind: str,
    attempts: list[Attempt],
    *,
    containers: ModalContainers | None = None,
    **overrides: str,
):
    containers = containers or ModalContainers()
    state = tmp_path / "state"
    state.mkdir()
    spec = {
        "submissions": [
            {
                "status": attempt.submission_status,
                "body": json.dumps(attempt.submission),
            }
            for attempt in attempts
        ],
        "records": {
            attempt.record_id: {
                "container": attempt.container,
                "before": attempt.reads_before_stop,
                "after": attempt.reads_after_stop or attempt.reads_before_stop[-1:],
            }
            for attempt in attempts
        },
        "calls": {
            attempt.call_id: {
                "container": attempt.container,
                "before": attempt.inputs_before_stop or [[]],
                "after": attempt.inputs_after_stop
                or attempt.inputs_before_stop
                or [[]],
            }
            for attempt in attempts
        },
        "listed": list(containers.listed),
        "refused_stops": list(containers.refused_stops),
    }
    (state / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
    environment = {
        "SIMULATION_TEST_AUTH_ISSUER": "https://issuer.example/",
        "SIMULATION_TEST_AUTH_AUDIENCE": "simulation-entry",
        "SIMULATION_TEST_AUTH_CLIENT_ID": "staging-test-client",
        "SIMULATION_TEST_AUTH_CLIENT_SECRET": CLIENT_SECRET,
        "MODAL_TOKEN_ID": "ak-test",
        "MODAL_TOKEN_SECRET": "as-test",
        "MODAL_ENVIRONMENT": "staging",
        "STAGING_HOUSEHOLD_ID": HOUSEHOLD_ID,
        "FAKE_STATE": str(state),
        "FAKE_BASE_URL": BASE_URL,
        "FAKE_ACCESS_TOKEN": ACCESS_TOKEN,
        **overrides,
    }
    result = run_script(
        CHECK_SCRIPT, kind, BASE_URL + "/", env=fakes.env(**environment)
    )
    return result, state


def _stops(fakes: FakeCommands) -> list[str]:
    """Return the containers the check asked Modal to stop, in order."""
    return [
        call[-1]
        for call in fakes.calls("uv")
        if call[1:6] == ["run", "--frozen", "modal", "container", "stop"]
    ]


def _submissions(state: Path) -> list[dict]:
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(state.glob("submission-*.json"))
    ]


def test_a_household_job_whose_container_is_stopped_succeeds_in_another(
    fakes, tmp_path
):
    result, state = _run(fakes, tmp_path, "household", [_household_attempt(0)])

    assert result.returncode == 0, result.stderr
    assert _submissions(state) == [{"country": "us", "household_id": HOUSEHOLD_ID}]
    assert _stops(fakes) == [CONTAINERS[0]]
    assert (state / "read-urls").read_text(encoding="utf-8").splitlines()[0] == (
        f"{BASE_URL}/household-calculations/{JOB_IDS[0]}"
    )
    assert CONTAINERS[0] in result.stdout
    assert RERUN_CONTAINER in result.stdout
    assert CALL_IDS[0] in result.stdout


def test_the_container_is_stopped_only_once_the_job_is_running(fakes, tmp_path):
    """The household function claims the job when it marks it running."""
    result, _ = _run(fakes, tmp_path, "household", [_household_attempt(0)])

    assert result.returncode == 0, result.stderr
    calls = fakes.calls()
    stop_index = next(
        index for index, call in enumerate(calls) if call[0] == "uv" and "stop" in call
    )
    reads_before_the_stop = [
        call
        for call in calls[:stop_index]
        if call[0] == "curl" and "--request" not in call
    ]
    # Pending without a call, pending with one, then running.
    assert len(reads_before_the_stop) == 3


def test_the_container_is_checked_to_run_in_the_staging_environment(fakes, tmp_path):
    result, _ = _run(fakes, tmp_path, "household", [_household_attempt(0)])

    assert result.returncode == 0, result.stderr
    listings = [
        call[1:]
        for call in fakes.calls("uv")
        if call[1:6] == ["run", "--frozen", "modal", "container", "list"]
    ]
    assert listings == [
        ["run", "--frozen", "modal", "container", "list", "--env", "staging", "--json"]
    ]


def test_a_container_modal_does_not_list_in_staging_is_never_stopped(fakes, tmp_path):
    result, _ = _run(
        fakes,
        tmp_path,
        "household",
        [_household_attempt(0)],
        containers=ModalContainers(listed=()),
        PREEMPTION_CHECK_ATTEMPTS="1",
    )

    assert result.returncode == 1
    assert _stops(fakes) == []
    assert f"{CONTAINERS[0]} is not listed in Modal environment staging" in (
        result.stderr
    )


def test_a_report_runs_coordinator_container_is_stopped_and_it_succeeds(
    fakes, tmp_path
):
    result, state = _run(fakes, tmp_path, "report", [_report_attempt(0)])

    assert result.returncode == 0, result.stderr
    assert _submissions(state) == [REPORT_REQUEST]
    assert _stops(fakes) == [CONTAINERS[0]]
    assert (state / "read-urls").read_text(encoding="utf-8").splitlines()[0] == (
        f"{BASE_URL}/reports/{REPORT_IDS[0]}"
    )
    assert REPORT_RUN_IDS[0] in result.stdout
    assert RERUN_CONTAINER in result.stdout


def test_a_report_whose_newest_run_is_not_the_created_one_fails(fakes, tmp_path):
    attempt = _report_attempt(0)
    attempt.reads_before_stop = [
        _report(0, "running", CALL_IDS[0], run_id=REPORT_RUN_IDS[1])
    ]

    result, _ = _run(fakes, tmp_path, "report", [attempt])

    assert result.returncode == 1
    assert REPORT_RUN_IDS[1] in result.stderr
    assert _stops(fakes) == []


def test_a_calculation_that_finishes_before_the_stop_is_submitted_again(
    fakes, tmp_path
):
    result, state = _run(
        fakes,
        tmp_path,
        "household",
        [_household_attempt(0, proves=False), _household_attempt(1)],
    )

    assert result.returncode == 0, result.stderr
    assert len(_submissions(state)) == 2
    assert _stops(fakes) == [CONTAINERS[1]]
    assert "finished before its container was stopped" in result.stderr


def test_three_calculations_that_finish_before_the_stop_fail_the_check(fakes, tmp_path):
    result, state = _run(
        fakes,
        tmp_path,
        "household",
        [_household_attempt(attempt, proves=False) for attempt in range(3)],
    )

    assert result.returncode == 1
    assert len(_submissions(state)) == 3
    assert _stops(fakes) == []
    assert "no attempt in 3" in result.stderr


def test_a_stop_modal_refuses_moves_on_to_another_attempt(fakes, tmp_path):
    result, state = _run(
        fakes,
        tmp_path,
        "household",
        [_household_attempt(0), _household_attempt(1)],
        containers=ModalContainers(refused_stops=(CONTAINERS[0],)),
    )

    assert result.returncode == 0, result.stderr
    assert len(_submissions(state)) == 2
    assert _stops(fakes) == [CONTAINERS[0], CONTAINERS[1]]
    assert f"could not stop container {CONTAINERS[0]}" in result.stderr


def test_a_call_that_finished_in_the_stopped_container_proves_nothing(fakes, tmp_path):
    """The input finished just before the stop reached its container."""
    attempt = _household_attempt(0)
    attempt.inputs_after_stop = [_inputs((CONTAINERS[0], "SUCCESS"))]

    result, _ = _run(
        fakes,
        tmp_path,
        "household",
        [attempt],
        PREEMPTION_CHECK_ATTEMPTS="1",
        PREEMPTION_CHECK_RERUN_SECONDS="0",
    )

    assert result.returncode == 1
    assert "never showed" in result.stderr
    assert CONTAINERS[0] in result.stderr


def test_the_call_graph_is_asked_again_until_it_shows_the_other_container(
    fakes, tmp_path
):
    """Modal records the call graph best-effort and not in real time."""
    attempt = _household_attempt(0)
    attempt.inputs_after_stop = [
        "ERROR",
        _inputs((CONTAINERS[0], "TERMINATED")),
        _inputs((CONTAINERS[0], "TERMINATED"), (RERUN_CONTAINER, "SUCCESS")),
    ]

    result, _ = _run(fakes, tmp_path, "household", [attempt])

    assert result.returncode == 0, result.stderr
    assert RERUN_CONTAINER in result.stdout


def test_the_call_graph_is_asked_again_until_it_names_the_running_container(
    fakes, tmp_path
):
    attempt = _household_attempt(0)
    attempt.reads_before_stop = [
        _household(JOB_IDS[0], "running", CALL_IDS[0]),
    ]
    attempt.inputs_before_stop = [
        "ERROR",
        _inputs(("", "PENDING")),
        _inputs((CONTAINERS[0], "PENDING")),
    ]

    result, _ = _run(fakes, tmp_path, "household", [attempt])

    assert result.returncode == 0, result.stderr
    assert _stops(fakes) == [CONTAINERS[0]]


def test_a_failed_calculation_fails_the_check_at_once(fakes, tmp_path):
    """A calculation that fails isn't submitted again: it would fail the same way."""
    attempt = _household_attempt(0)
    attempt.reads_after_stop = [
        _household(
            JOB_IDS[0],
            "failed",
            CALL_IDS[0],
            error_message="household_function_error: the claim was lost",
        )
    ]

    result, state = _run(fakes, tmp_path, "household", [attempt, _household_attempt(1)])

    assert result.returncode == 1
    assert len(_submissions(state)) == 1
    assert "household_function_error: the claim was lost" in result.stderr
    assert f"after its container {CONTAINERS[0]} was stopped" in result.stderr


def test_a_calculation_under_another_call_after_the_stop_fails(fakes, tmp_path):
    """Modal runs a stopped container's input again under the same call ID."""
    attempt = _household_attempt(0)
    attempt.reads_after_stop = [_household(JOB_IDS[0], "succeeded", "fc-01OTHER")]

    result, _ = _run(fakes, tmp_path, "household", [attempt])

    assert result.returncode == 1
    assert "fc-01OTHER" in result.stderr
    assert CALL_IDS[0] in result.stderr


def test_a_calculation_that_does_not_finish_in_time_fails(fakes, tmp_path):
    attempt = _household_attempt(0)
    attempt.reads_after_stop = [_household(JOB_IDS[0], "running", CALL_IDS[0])]

    result, _ = _run(
        fakes,
        tmp_path,
        "household",
        [attempt],
        PREEMPTION_CHECK_DEADLINE_SECONDS="0",
    )

    assert result.returncode == 1
    assert "did not finish within 0 seconds" in result.stderr
    assert "last read: the household job was" in result.stderr


@pytest.mark.parametrize(
    "answer",
    ["TRANSPORT", '503 {"code": "x"}', "429 {}"],
    ids=["no-answer", "unavailable", "too-many-requests"],
)
def test_a_read_the_service_cannot_answer_now_is_asked_again(fakes, tmp_path, answer):
    attempt = _household_attempt(0)
    attempt.reads_before_stop = [answer, *attempt.reads_before_stop]

    result, _ = _run(fakes, tmp_path, "household", [attempt])

    assert result.returncode == 0, result.stderr


def test_any_other_read_answer_fails_the_check(fakes, tmp_path):
    attempt = _household_attempt(0)
    attempt.reads_before_stop = ['404 {"code": "unknown_household_job"}']

    result, _ = _run(fakes, tmp_path, "household", [attempt])

    assert result.returncode == 1
    assert "404" in result.stderr


@pytest.mark.parametrize(
    ("kind", "status", "body"),
    [
        ("household", "503", {"code": "modal_call_refused", "detail": "x"}),
        ("report", "400", {"code": "unknown_region", "detail": "x"}),
    ],
)
def test_a_refused_submission_fails_the_check_with_its_answer(
    fakes, tmp_path, kind, status, body
):
    attempt = _household_attempt(0) if kind == "household" else _report_attempt(0)
    attempt.submission_status = status
    attempt.submission = body

    result, state = _run(fakes, tmp_path, kind, [attempt])

    assert result.returncode == 1
    assert status in result.stderr
    assert body["code"] in result.stderr
    assert not (state / "read-urls").exists()


@pytest.mark.parametrize(
    "call_id",
    ["fc-01CALL1 --yes", "--help", "ta-01CONTAINER1", "fc-"],
    ids=["extra-argument", "option", "container-id", "empty-suffix"],
)
def test_a_recorded_call_that_is_not_a_modal_call_id_fails_the_check(
    fakes, tmp_path, call_id
):
    attempt = _household_attempt(0)
    attempt.reads_before_stop = [_household(JOB_IDS[0], "running", call_id)]

    result, _ = _run(fakes, tmp_path, "household", [attempt])

    assert result.returncode == 1
    assert "not a Modal call ID" in result.stderr
    assert fakes.calls("uv") == []


def test_a_container_that_is_not_a_modal_container_id_is_never_stopped(fakes, tmp_path):
    attempt = _household_attempt(0)
    attempt.inputs_before_stop = [_inputs(("--all", "PENDING"))]

    result, _ = _run(fakes, tmp_path, "household", [attempt])

    assert result.returncode == 1
    assert "not a Modal container ID" in result.stderr
    assert _stops(fakes) == []


def test_no_secret_reaches_a_command_line(fakes, tmp_path):
    """The token is sent from a file, and the client secret on standard input."""
    result, _ = _run(fakes, tmp_path, "household", [_household_attempt(0)])

    assert result.returncode == 0, result.stderr
    arguments = [argument for call in fakes.calls() for argument in call]
    assert not any(ACCESS_TOKEN in argument for argument in arguments)
    assert not any(CLIENT_SECRET in argument for argument in arguments)


def test_every_request_has_a_time_limit(fakes, tmp_path):
    """The submission, which isn't asked again, may take Cloud Run's full minute."""
    result, _ = _run(fakes, tmp_path, "report", [_report_attempt(0)])

    assert result.returncode == 0, result.stderr
    limits = {
        call[-1]: call[call.index("--max-time") + 1] for call in fakes.calls("curl")
    }
    assert limits == {
        "https://issuer.example/oauth/token": "30",
        f"{BASE_URL}/reports": "60",
        f"{BASE_URL}/reports/{REPORT_IDS[0]}": "30",
    }


def test_the_check_runs_only_in_modals_staging_environment(fakes, tmp_path):
    """It stops containers, so it never runs against production's."""
    result, _ = _run(
        fakes, tmp_path, "household", [_household_attempt(0)], MODAL_ENVIRONMENT="main"
    )

    assert result.returncode == 1
    assert "staging" in result.stderr
    assert fakes.calls() == []


@pytest.mark.parametrize(
    "variable_name",
    [
        "SIMULATION_TEST_AUTH_CLIENT_ID",
        "MODAL_TOKEN_ID",
        "MODAL_TOKEN_SECRET",
        "MODAL_ENVIRONMENT",
        "STAGING_HOUSEHOLD_ID",
    ],
)
def test_the_household_check_needs_its_settings(fakes, tmp_path, variable_name):
    result, _ = _run(
        fakes, tmp_path, "household", [_household_attempt(0)], **{variable_name: ""}
    )

    assert result.returncode == 1
    assert variable_name in result.stderr
    assert fakes.calls() == []


def test_the_report_check_needs_no_household(fakes, tmp_path):
    result, _ = _run(
        fakes, tmp_path, "report", [_report_attempt(0)], STAGING_HOUSEHOLD_ID=""
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("variable_name", "value"),
    [
        ("STAGING_HOUSEHOLD_ID", "not-a-uuid"),
        ("PREEMPTION_CHECK_ATTEMPTS", "0"),
        ("PREEMPTION_CHECK_ATTEMPTS", "three"),
        ("PREEMPTION_CHECK_DEADLINE_SECONDS", "ten"),
        ("PREEMPTION_CHECK_POLL_SECONDS", "-1"),
        ("PREEMPTION_CHECK_RERUN_SECONDS", "2m"),
    ],
)
def test_a_malformed_setting_fails_the_check_before_any_request(
    fakes, tmp_path, variable_name, value
):
    result, _ = _run(
        fakes, tmp_path, "household", [_household_attempt(0)], **{variable_name: value}
    )

    assert result.returncode == 1
    assert variable_name in result.stderr
    assert fakes.calls() == []


def test_an_unknown_kind_of_calculation_is_refused(fakes, tmp_path):
    result, _ = _run(fakes, tmp_path, "simulation", [_household_attempt(0)])

    assert result.returncode == 1
    assert "household or report" in result.stderr
    assert fakes.calls() == []


# --- The workflow job --------------------------------------------------------

CHECK_ENVIRONMENT = {
    "SIMULATION_TEST_AUTH_ISSUER": "${{ secrets.SIMULATION_ENTRYPOINT_AUTH_ISSUER }}",
    "SIMULATION_TEST_AUTH_AUDIENCE": (
        "${{ secrets.SIMULATION_ENTRYPOINT_AUTH_AUDIENCE }}"
    ),
    "SIMULATION_TEST_AUTH_CLIENT_ID": (
        "${{ secrets.SIMULATION_ENTRYPOINT_TEST_AUTH_CLIENT_ID }}"
    ),
    "SIMULATION_TEST_AUTH_CLIENT_SECRET": (
        "${{ secrets.SIMULATION_ENTRYPOINT_TEST_AUTH_CLIENT_SECRET }}"
    ),
    "MODAL_TOKEN_ID": "${{ secrets.MODAL_TOKEN_ID }}",
    "MODAL_TOKEN_SECRET": "${{ secrets.MODAL_TOKEN_SECRET }}",
    "MODAL_ENVIRONMENT": "${{ inputs.modal_environment }}",
}


def _jobs() -> dict:
    return workflow_jobs(REUSABLE_DEPLOY_WORKFLOW)


def _context(**overrides):
    context = routing_context()
    context["inputs"]["deployment_environment"] = "staging"
    context["inputs"]["deploy_stage12_v2"] = True
    context["needs"]["deploy_entrypoint"] = {"result": "success"}
    context["needs"]["publish_stage12_v2_default"] = {"result": "success"}
    context["needs"]["staging_economy_check"] = {"result": "success"}
    for path, value in overrides.items():
        target = context
        *parents, last = path.split("__")
        for part in parents:
            target = target[part]
        target[last] = value
    return context


def test_the_check_job_stops_a_household_and_a_report_through_the_candidate():
    job = _jobs()[CHECK_JOB]
    run_steps = [step for step in job["steps"] if "run" in step]
    install, household, report = run_steps

    assert set(job["needs"]) == {
        "deploy_entrypoint",
        "authenticated_test",
        "publish_stage12_v2_default",
        "staging_economy_check",
    }
    assert job["environment"] == "${{ inputs.release_environment }}"
    # Longer than both steps' own deadlines together, so the script fails first.
    assert job["timeout-minutes"] == 100
    assert {step.get("uses") for step in job["steps"] if "uses" in step} == {
        "actions/checkout@v6",
        "actions/setup-python@v6",
        "astral-sh/setup-uv@v8.1.0",
    }
    for step in run_steps:
        assert step["working-directory"] == "projects/policyengine-simulation-executor"
    assert install["run"] == "uv sync --frozen"
    candidate = '"${{ needs.deploy_entrypoint.outputs.candidate_url }}"'
    assert household["run"] == (
        f"../../.github/scripts/{CHECK_SCRIPT} household {candidate}"
    )
    assert household["env"] == {
        **CHECK_ENVIRONMENT,
        "STAGING_HOUSEHOLD_ID": "${{ vars.SIMULATION_STAGING_HOUSEHOLD_ID }}",
    }
    assert report["run"] == f"../../.github/scripts/{CHECK_SCRIPT} report {candidate}"
    assert report["env"] == CHECK_ENVIRONMENT


@pytest.mark.parametrize(
    ("overrides", "runs"),
    [
        ({}, True),
        ({"inputs__deployment_environment": "production"}, False),
        ({"needs__deploy_entrypoint__result": "failure"}, False),
        ({"needs__authenticated_test__result": "failure"}, False),
        ({"needs__staging_economy_check__result": "failure"}, False),
        ({"needs__staging_economy_check__result": "skipped"}, False),
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
        "economy-check-failed",
        "economy-check-skipped",
        "default-move-failed",
        "default-move-skipped",
        "no-stage12",
        "cancelled",
    ],
)
def test_the_check_runs_on_every_staging_deploy_after_the_economy_check(
    overrides, runs
):
    job = _jobs()[CHECK_JOB]

    assert (
        bool(evaluate_github_expression(job["if"], _context(**overrides), job_if=True))
        is runs
    )


def test_deployment_ready_requires_the_staging_preemption_check():
    ready = _jobs()["deployment_ready"]
    (step,) = [
        step
        for step in ready["steps"]
        if step.get("name") == "Check the staging preemption check result"
    ]

    assert CHECK_JOB in ready["needs"]
    assert step["run"] == f".github/scripts/{READY_SCRIPT}"
    assert step["env"] == {
        "REQUESTED": "${{ inputs.deployment_environment == 'staging' }}",
        "CHECK_RESULT": "${{ needs.staging_preemption_check.result }}",
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
