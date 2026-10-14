"""The numerical release sentinel (src.modal.utils.release_sentinel).

Invariants:
- the in-process run receives exactly the payload the gateway forwards to the
  worker for the same request (differential against the gateway's model);
- a deployment passes only when it serves the expected policyengine version,
  UK model and dataset SHA-256 and every value agrees within tolerance;
- agreement is symmetric and admits only floating-point reordering: values
  within REL_TOL pass and anything beyond fails;
- a failed, missing or late job, or a malformed result, is a failure, never a
  pass.
"""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from src.modal.utils import release_sentinel as sentinel

IDENTITY = {
    "policyengine_version": "6.2.6",
    "uk": {
        "model_version": "2.125.1",
        "dataset": "enhanced_frs_2024_25",
        "dataset_sha256": "5" * 64,
    },
}
RESULT = {
    "budget": {
        "budgetary_impact": 6.1e9,
        "tax_revenue_impact": 6.3e9,
        "benefit_spending_impact": 0.2e9,
        "households": 2.9e7,
        "baseline_net_income": 1.4e12,
    },
    "poverty": {},
}


def _expected():
    received = []

    def runner(payload):
        received.append(payload)
        return RESULT

    expected = sentinel.compute_expected(runner=runner, identity=IDENTITY)
    return expected, received


def test_expected_values_come_from_the_gateway_payload():
    from policyengine_simulation_contract.gateway_models import SimulationRequest

    expected, received = _expected()
    request = sentinel.SENTINEL_CASES[0]["request"]
    gateway_payload = SimulationRequest.model_validate(
        {**request, "policyengine_version": "6.2.6", "version": "2.125.1"}
    ).model_dump(
        exclude={"version", "policyengine_version", "telemetry"},
        mode="json",
        exclude_none=True,
    )
    assert received == [gateway_payload]
    assert expected["schema"] == sentinel.SCHEMA
    assert expected["cases"][0]["values"]["budget.budgetary_impact"] == 6.1e9
    assert json.loads(json.dumps(expected)) == expected


def test_expected_defaults_to_the_worker_implementation_and_local_runtime(monkeypatch):
    runtime = object()
    created_runtimes = []
    runs = []

    def local_runtime():
        created_runtimes.append(runtime)
        return runtime

    def run_simulation(payload, *, runtime):
        runs.append((payload, runtime))
        return RESULT

    monkeypatch.setitem(
        sys.modules,
        "policyengine_simulation_executor.stage12_qualification",
        SimpleNamespace(_local_qualification_runtime=local_runtime),
    )
    monkeypatch.setitem(
        sys.modules,
        "policyengine_simulation_executor.simulation_runtime",
        SimpleNamespace(run_simulation_impl=run_simulation),
    )

    expected = sentinel.compute_expected(identity=IDENTITY)

    assert created_runtimes == [runtime]
    assert runs == [
        (sentinel.worker_payload(sentinel.SENTINEL_CASES[0]["request"]), runtime)
    ]
    assert expected["cases"][0]["values"] == {
        path: RESULT["budget"][path.split(".")[1]] for path in COMPARED
    }


def test_expected_defaults_to_the_installed_package_and_uk_bundle_identity(monkeypatch):
    distributions = []
    countries = []
    uk = SimpleNamespace(
        model_version="2.126.4",
        default_dataset="enhanced_frs_2025_26",
        default_dataset_sha256="a" * 64,
    )

    def package_version(distribution):
        distributions.append(distribution)
        return "6.2.7"

    def country_bundle(country):
        countries.append(country)
        return uk

    monkeypatch.setattr(sentinel.metadata, "version", package_version)
    monkeypatch.setitem(
        sys.modules,
        "policyengine_simulation_executor.release_bundle",
        SimpleNamespace(get_country_release_bundle=country_bundle),
    )

    expected = sentinel.compute_expected(runner=lambda _payload: RESULT)

    assert distributions == ["policyengine"]
    assert countries == ["uk"]
    assert expected["policyengine_version"] == "6.2.7"
    assert expected["uk"] == {
        "model_version": uk.model_version,
        "dataset": uk.default_dataset,
        "dataset_sha256": uk.default_dataset_sha256,
    }


def test_expected_needs_the_bundle_dataset_digest():
    identity = {**IDENTITY, "uk": {**IDENTITY["uk"], "dataset_sha256": None}}
    with pytest.raises(sentinel.SentinelError, match="dataset SHA-256"):
        sentinel.compute_expected(runner=lambda _payload: RESULT, identity=identity)


class FakeAPI:
    """Submit returns a job; polling returns 202 ``pending`` times, then a result."""

    def __init__(
        self, *, bundle=None, result=RESULT, pending=2, job_status=200, job=None
    ):
        #: ``job`` replaces the whole completed job body when given.
        self.job = job
        self.bundle = {
            "policyengine_version": "6.2.6",
            "model_version": "2.125.1",
            "dataset_sha256": "5" * 64,
            **(bundle or {}),
        }
        self.result = result
        self.pending = pending
        self.job_status = job_status
        self.calls = []

    def __call__(self, method, url, *, token=None, body=None):
        self.calls.append((method, url, token, body))
        if method == "POST":
            return 200, {}, {"job_id": "fc-1", "policyengine_bundle": self.bundle}
        if self.pending:
            self.pending -= 1
            return 202, {"Retry-After": "1"}, {"status": "submitted"}
        if self.job_status != 200:
            return self.job_status, {}, {"status": "failed", "error": "boom"}
        if self.job is not None:
            return 200, {}, self.job
        return 200, {}, {"status": "complete", "result": self.result}


def _check(api, **kwargs):
    expected, _ = _expected()
    sleeps = []
    problems = sentinel.check_deployment(
        "https://candidate.example/",
        expected,
        token="t0ken",
        http=api,
        sleep=sleeps.append,
        **kwargs,
    )
    return problems, sleeps


def test_a_deployment_that_reproduces_the_numbers_passes():
    api = FakeAPI()
    problems, sleeps = _check(api)
    assert problems == []
    method, url, token, body = api.calls[0]
    assert (method, url, token) == (
        "POST",
        "https://candidate.example/simulate/economy/comparison",
        "t0ken",
    )
    assert body["policyengine_version"] == "6.2.6"
    assert sleeps == [1.0, 1.0]


@pytest.mark.parametrize(
    ("api", "fragment"),
    [
        (
            FakeAPI(bundle={"policyengine_version": "6.2.3"}),
            "served policyengine_version",
        ),
        (FakeAPI(bundle={"model_version": "2.102.3"}), "served model_version"),
        (FakeAPI(bundle={"dataset_sha256": "e" * 64}), "served dataset_sha256"),
        (
            FakeAPI(result={"budget": {**RESULT["budget"], "budgetary_impact": 6.2e9}}),
            "budget.budgetary_impact served",
        ),
        (FakeAPI(result={"budget": {}}), "the result has no budget.budgetary_impact"),
        (
            FakeAPI(
                result={"budget": {**RESULT["budget"], "baseline_net_income": "many"}}
            ),
            "not a number",
        ),
        (FakeAPI(job_status=500), "job returned HTTP 500"),
    ],
    ids=["wrapper", "model", "dataset", "value", "missing", "type", "failed job"],
)
def test_any_disagreement_fails(api, fragment):
    problems, _ = _check(api)
    assert any(fragment in problem for problem in problems), problems


LABEL = sentinel.SENTINEL_CASES[0]["case_id"]
COMPARED = (
    "budget.budgetary_impact",
    "budget.tax_revenue_impact",
    "budget.benefit_spending_impact",
    "budget.baseline_net_income",
)


def test_the_sentinel_compares_the_budget_and_net_income():
    assert tuple(sentinel.SENTINEL_CASES[0]["values"]) == COMPARED


@pytest.mark.parametrize("path", COMPARED)
def test_every_compared_value_must_agree(path):
    """#744 review: removing any one comparison must fail a test."""
    section, key = path.split(".")
    result = {**RESULT, section: {**RESULT[section], key: RESULT[section][key] * 1.01}}
    problems, _ = _check(FakeAPI(result=result))
    assert problems == [
        (
            f"{LABEL}: {path} served {result[section][key]!r}, "
            f"computed {RESULT[section][key]!r}"
        )
    ]


@pytest.mark.parametrize("value", [True, False, "6.1e9", None, [6.1e9]])
def test_a_value_that_is_not_a_number_fails(value):
    """#744 review: a boolean is not a number, whatever Python says."""
    result = {**RESULT, "budget": {**RESULT["budget"], "budgetary_impact": value}}
    problems, _ = _check(FakeAPI(result=result))
    assert any("not a number" in problem for problem in problems), problems


@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan")])
def test_a_non_finite_value_fails(value):
    result = {**RESULT, "budget": {**RESULT["budget"], "budgetary_impact": value}}
    problems, _ = _check(FakeAPI(result=result))
    assert any("not a finite number" in problem for problem in problems), problems


@pytest.mark.parametrize("status", ["failed", "cancelled", None, "COMPLETE"])
def test_a_job_that_did_not_complete_fails_even_with_the_right_numbers(status):
    problems, _ = _check(FakeAPI(job={"status": status, "result": RESULT}))
    assert problems == [f"{LABEL}: job ended {status!r}"]


@pytest.mark.parametrize(
    "job",
    [
        {"status": "complete"},
        {"status": "complete", "result": None},
        {"status": "complete", "result": []},
        {"status": "complete", "result": "done"},
        {"status": "complete", "result": 6.1e9},
    ],
    ids=["missing", "null", "list", "string", "number"],
)
def test_a_complete_job_without_an_object_result_fails(job):
    """#744 review: a worker returning None must not pass the sentinel."""
    problems, _ = _check(FakeAPI(job=job))
    assert len(problems) == 1
    assert "result, not an object" in problems[0]


@pytest.mark.parametrize(
    ("relative", "agrees"), [(1e-10, True), (5e-10, True), (2e-9, False), (1e-6, False)]
)
def test_the_tolerance_is_floating_point_reordering_only(relative, agrees):
    """#744 review: a looser REL_TOL (a 1e-3 mismatch is £4m) must fail a test."""
    value = 4e9
    assert sentinel.agree(value, value * (1 + relative)) is agrees


@pytest.mark.parametrize(
    ("actual", "agrees"),
    [(0.009, True), (0.01, True), (-0.01, True), (0.011, False)],
)
def test_the_absolute_tolerance_is_one_penny(actual, agrees):
    """Near zero, numerical agreement admits up to and including £0.01 of error."""
    assert sentinel.agree(0, actual) is agrees
    assert sentinel.agree(actual, 0) is agrees


def test_a_job_that_never_finishes_fails():
    ticks = iter(range(0, 10_000, 100))
    problems, _ = _check(
        FakeAPI(pending=10_000), timeout_seconds=250, clock=lambda: next(ticks)
    )
    assert any("no result within 250s" in problem for problem in problems)


def test_a_rejected_submit_fails():
    def api(_method, _url, **_kwargs):
        return 400, {}, {"detail": "unknown policyengine_version"}

    problems, _ = _check(api)
    assert problems
    assert "submit returned HTTP 400" in problems[0]


def test_an_expected_file_of_another_schema_is_refused():
    with pytest.raises(sentinel.SentinelError):
        sentinel.check_deployment("https://x", {"schema": "other", "cases": []})


@settings(max_examples=200, deadline=None)
@given(
    value=st.floats(min_value=-1e13, max_value=1e13, allow_nan=False),
    factor=st.floats(min_value=0, max_value=0.99),
)
def test_agreement_admits_only_reordering_error(value, factor):
    tolerance = max(sentinel.ABS_TOL, sentinel.REL_TOL * abs(value))
    near = value + factor * tolerance * 0.5
    assert sentinel.agree(value, near)
    assert sentinel.agree(near, value)
    far = value + 3 * tolerance
    assert not sentinel.agree(value, far)
    assert not sentinel.agree(far, value)


def test_access_token_follows_the_auth_configuration(monkeypatch):
    assert sentinel.access_token({}) is None
    with pytest.raises(sentinel.SentinelError, match="REQUIRED"):
        sentinel.access_token({"SIMULATION_TEST_AUTH_REQUIRED": "1"})
    with pytest.raises(sentinel.SentinelError, match="partial"):
        sentinel.access_token({"SIMULATION_TEST_AUTH_ISSUER": "https://issuer"})
    seen = {}

    def http(method, url, *, body=None, **_kwargs):
        seen.update(method=method, url=url, body=body)
        return 200, {}, {"access_token": "minted"}

    monkeypatch.setattr(sentinel, "_http", http)
    environ = dict.fromkeys(sentinel._AUTH_VARS, "x")
    environ["SIMULATION_TEST_AUTH_ISSUER"] = "https://issuer/"
    assert sentinel.access_token(environ) == "minted"
    assert seen["url"] == "https://issuer/oauth/token"
    assert seen["body"]["grant_type"] == "client_credentials"


def test_main_check_exit_codes(tmp_path, monkeypatch, capsys):
    expected, _ = _expected()
    path = tmp_path / "sentinel.json"
    path.write_text(json.dumps(expected))
    monkeypatch.setattr(sentinel, "access_token", lambda: None)
    monkeypatch.setattr(sentinel, "_http", FakeAPI(pending=0))
    assert (
        sentinel.main(["check", "--base-url", "https://x", "--expected", str(path)])
        == 0
    )
    monkeypatch.setattr(
        sentinel,
        "_http",
        FakeAPI(pending=0, bundle={"dataset_sha256": "e" * 64}),
    )
    assert (
        sentinel.main(["check", "--base-url", "https://x", "--expected", str(path)])
        == 1
    )
    assert "does not reproduce" in capsys.readouterr().err


# --- Characterization: check_deployment's problem order and polling ----------


class ScriptedAPI:
    """Answer each call with the next scripted response and record the call."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, *, token=None, body=None):
        self.calls.append((method, url, token, body))
        return self.responses.pop(0)


SERVED_BUNDLE = {
    "policyengine_version": "6.2.6",
    "model_version": "2.125.1",
    "dataset_sha256": "5" * 64,
}
SUBMITTED = (200, {}, {"job_id": "fc-1", "policyengine_bundle": SERVED_BUNDLE})
COMPLETE = (200, {}, {"status": "complete", "result": RESULT})


def _check_scripted(api, expected=None, **kwargs):
    if expected is None:
        expected, _ = _expected()
    sleeps = []
    problems = sentinel.check_deployment(
        "https://candidate.example/",
        expected,
        token="t0ken",  # noqa: S106  # fake bearer token; the HTTP calls are stubbed
        http=api,
        sleep=sleeps.append,
        **kwargs,
    )
    return problems, sleeps


def test_check_submits_then_polls_the_job_url_with_the_token():
    api = ScriptedAPI(SUBMITTED, (202, {}, {"status": "submitted"}), COMPLETE)

    problems, sleeps = _check_scripted(api, poll_seconds=7)

    assert problems == []
    assert sleeps == [7]
    expected, _ = _expected()
    case = expected["cases"][0]
    assert api.calls == [
        (
            "POST",
            "https://candidate.example/simulate/economy/comparison",
            "t0ken",
            {**case["request"], "policyengine_version": "6.2.6"},
        ),
        ("GET", "https://candidate.example/jobs/fc-1", "t0ken", None),
        ("GET", "https://candidate.example/jobs/fc-1", "t0ken", None),
    ]


@pytest.mark.parametrize(
    ("headers", "slept"),
    [
        ({"Retry-After": "3"}, 3.0),
        ({"retry-after": "4.5"}, 4.5),
        ({"RETRY-AFTER": "-2"}, 0.0),
        ({"Retry-After": "soon"}, 7),
        ({}, 7),
    ],
    ids=["header", "lowercase", "negative", "unparseable", "missing"],
)
@pytest.mark.parametrize("status", [202, 429, 503])
def test_check_waits_for_a_pending_job_as_retry_after_says(status, headers, slept):
    api = ScriptedAPI(SUBMITTED, (status, headers, {"status": "submitted"}), COMPLETE)

    problems, sleeps = _check_scripted(api, poll_seconds=7)

    assert problems == []
    assert sleeps == [slept]


def test_check_reports_identity_problems_before_value_problems():
    served = {"policyengine_version": "6.2.3", "model_version": "2.102.3"}
    result = {**RESULT, "budget": {**RESULT["budget"], "budgetary_impact": 6.2e9}}
    api = ScriptedAPI(
        (200, {}, {"job_id": "fc-1", "policyengine_bundle": served}),
        (200, {}, {"status": "complete", "result": result}),
    )

    problems, _ = _check_scripted(api)

    assert problems == [
        f"{LABEL}: served policyengine_version '6.2.3', expected '6.2.6'",
        f"{LABEL}: served model_version '2.102.3', expected '2.125.1'",
        f"{LABEL}: served dataset_sha256 None, expected '{'5' * 64}'",
        f"{LABEL}: budget.budgetary_impact served 6200000000.0, computed 6100000000.0",
    ]


def test_check_reports_a_submit_without_a_bundle_as_three_identity_problems():
    api = ScriptedAPI((200, {}, {"job_id": "fc-1"}), COMPLETE)

    problems, _ = _check_scripted(api)

    assert problems == [
        f"{LABEL}: served policyengine_version None, expected '6.2.6'",
        f"{LABEL}: served model_version None, expected '2.125.1'",
        f"{LABEL}: served dataset_sha256 None, expected '{'5' * 64}'",
    ]


def test_check_reports_a_submit_that_returned_no_object():
    api = ScriptedAPI((200, {}, ["job"]))

    problems, sleeps = _check_scripted(api)

    assert problems == [f"{LABEL}: submit returned HTTP 200: ['job']"]
    assert sleeps == []
    assert len(api.calls) == 1


def test_check_reports_a_job_body_that_is_not_an_object():
    api = ScriptedAPI(SUBMITTED, (200, {}, "complete"))

    problems, _ = _check_scripted(api)

    assert problems == [f"{LABEL}: job returned HTTP 200: complete"]


def test_check_gives_up_once_the_deadline_has_passed():
    ticks = iter([0, 100, 200, 300])
    pending = (202, {}, {"status": "submitted"})
    api = ScriptedAPI(SUBMITTED, pending, pending, pending)

    problems, sleeps = _check_scripted(
        api, timeout_seconds=250, poll_seconds=5, clock=lambda: next(ticks)
    )

    assert problems == [f"{LABEL}: no result within 250s"]
    assert sleeps == [5, 5]
    assert len(api.calls) == 4


def test_check_still_compares_the_object_result_of_a_failed_job():
    api = ScriptedAPI(SUBMITTED, (200, {}, {"status": "failed", "result": {}}))

    problems, _ = _check_scripted(api)

    assert problems == [
        f"{LABEL}: job ended 'failed'",
        *(f"{LABEL}: the result has no {path}" for path in COMPARED),
    ]


def test_check_continues_with_the_next_case_after_a_failed_one():
    expected, _ = _expected()
    second = {**expected["cases"][0], "case_id": "second"}
    expected = {**expected, "cases": [expected["cases"][0], second]}
    api = ScriptedAPI((500, {}, {"detail": "boom"}), SUBMITTED, COMPLETE)

    problems, _ = _check_scripted(api, expected=expected)

    assert problems == [f"{LABEL}: submit returned HTTP 500: {{'detail': 'boom'}}"]
    assert [call[0] for call in api.calls] == ["POST", "POST", "GET"]


def test_check_refuses_another_schema_before_any_request():
    api = ScriptedAPI()

    with pytest.raises(
        sentinel.SentinelError, match=r"^expected file schema is 'other'$"
    ):
        sentinel.check_deployment("https://x", {"schema": "other"}, http=api)

    assert api.calls == []


def test_access_token_refuses_a_failed_or_empty_token_response(monkeypatch):
    environ = dict.fromkeys(sentinel._AUTH_VARS, "x")
    for response in ((401, {}, {"access_token": "minted"}), (200, {}, {})):
        monkeypatch.setattr(
            sentinel, "_http", lambda *_args, answer=response, **_kwargs: answer
        )
        with pytest.raises(
            sentinel.SentinelError,
            match=rf"^token request failed with HTTP {response[0]}$",
        ):
            sentinel.access_token(environ)
