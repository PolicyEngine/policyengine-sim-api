"""The household calculation routes, with the service faked."""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, NoReturn
from uuid import UUID

import pytest
from conftest import FakeBackend, make_settings
from fastapi.testclient import TestClient
from policyengine_observability import TelemetryLimits
from policyengine_simulation_entry import app as app_module
from policyengine_simulation_entry.app import create_app
from policyengine_simulation_entry.household_api_models import (
    HouseholdCalculationAccepted,
    HouseholdCalculationRequest,
)
from policyengine_simulation_entry.household_calculations import (
    HouseholdCalculationInternalError,
    HouseholdCalculationRefusedError,
    HouseholdCalculationService,
    HouseholdCalculationUnavailableError,
    HouseholdCalculationUnprocessableError,
    UnprocessableReason,
)
from policyengine_simulation_entry.simulation_database import SimulationDatabase
from policyengine_simulation_observability.identifiers import OBSERVABILITY_ID_HEADER
from policyengine_simulation_persistence import HouseholdJobRecord, HouseholdJobStatus

if TYPE_CHECKING:
    from collections.abc import Callable

JOB_ID = UUID("00000000-0000-4000-8000-0000000000aa")
HOUSEHOLD_ID = UUID("00000000-0000-4000-8000-0000000000bb")
CATALOG_VERSION_ID = UUID("00000000-0000-4000-8000-0000000000dd")
OBSERVABILITY_ID = "00000000-0000-4000-8000-000000000001"
SUBMISSION = {"country": "us", "household_id": str(HOUSEHOLD_ID)}
RESULT_DOCUMENT = {
    "status": "succeeded",
    "schema_version": 1,
    "calculation_id": str(JOB_ID),
}
STATE_PATH = f"/household-calculations/{JOB_ID}"
STATE_TEMPLATE = "/household-calculations/{household_job_id}"


def _record(**changes: Any) -> HouseholdJobRecord:
    values = {
        "id": JOB_ID,
        "status": HouseholdJobStatus.SUCCEEDED,
        "country": "us",
        "household_id": HOUSEHOLD_ID,
        "policy_id": None,
        "year": 2026,
        "request_data": SUBMISSION,
        "tax_benefit_model_version_id": CATALOG_VERSION_ID,
        "country_package_version": "2.2.1",
        "policyengine_version": "6.17.1",
        "modal_call_id": "fc-1",
        "result": RESULT_DOCUMENT,
        "error_message": None,
        "created_at": datetime(2026, 11, 10, 12, tzinfo=UTC),
        "started_at": datetime(2026, 11, 10, 12, 0, 5, tzinfo=UTC),
        "completed_at": datetime(2026, 11, 10, 12, 0, 30, tzinfo=UTC),
        **changes,
    }
    return HouseholdJobRecord(**values)


def _raise_from_nested_calls(failure: Exception, depth: int) -> NoReturn:
    """Raise ``failure`` from ``depth`` calls below this one."""
    if depth == 0:
        raise failure
    _raise_from_nested_calls(failure, depth - 1)


@dataclass
class FakeHouseholdCalculations:
    """Records each submission and read, and answers as told.

    ``failure`` is raised by ``raise_failure(failure, failure_depth)``: by
    default from ``failure_depth`` nested calls below the first call of
    ``_raise_from_nested_calls``.
    """

    failure: Exception | None = None
    failure_depth: int = 0
    raise_failure: Callable[[Exception, int], NoReturn] = _raise_from_nested_calls
    stored: HouseholdJobRecord | None = None
    submissions: list[tuple[HouseholdCalculationRequest, Any]] = field(
        default_factory=list
    )
    reads: list[UUID] = field(default_factory=list)

    async def submit(
        self,
        request: HouseholdCalculationRequest,
        *,
        observability_context: dict[str, Any] | None,
    ) -> HouseholdCalculationAccepted:
        self.submissions.append((request, observability_context))
        if self.failure is not None:
            self.raise_failure(self.failure, self.failure_depth)
        return HouseholdCalculationAccepted(
            job_id=JOB_ID, status=HouseholdJobStatus.PENDING
        )

    async def read(self, job_id: UUID) -> HouseholdJobRecord | None:
        self.reads.append(job_id)
        if self.failure is not None:
            self.raise_failure(self.failure, self.failure_depth)
        return self.stored if self.stored and self.stored.id == job_id else None


@pytest.fixture
def client_for(monkeypatch):
    """Return a function building a client whose app uses ``calculations``.

    None leaves the app as an unconfigured service builds it.
    """

    def build(calculations: FakeHouseholdCalculations | None) -> TestClient:
        if calculations is not None:
            monkeypatch.setattr(
                app_module,
                "household_calculations_for",
                lambda settings, database: calculations,
            )
        return TestClient(
            create_app(
                settings=make_settings(),
                backend=FakeBackend(),
                auth_dependency=lambda: None,
            )
        )

    return build


def test_a_submission_answers_202_with_the_job_and_its_status(client_for):
    calculations = FakeHouseholdCalculations()

    with client_for(calculations) as client:
        result = client.post(
            "/household-calculations",
            json={
                **SUBMISSION,
                "policy_id": None,
                "add_missing_variables": True,
                "spm": {"scenario": "zero_real"},
            },
            headers={OBSERVABILITY_ID_HEADER: OBSERVABILITY_ID},
        )

    assert result.status_code == 202
    assert result.json() == {"job_id": str(JOB_ID), "status": "pending"}
    assert result.headers[OBSERVABILITY_ID_HEADER] == OBSERVABILITY_ID
    ((request, context),) = calculations.submissions
    assert request.household_id == HOUSEHOLD_ID
    assert request.add_missing_variables is True
    assert request.spm_requested is True
    assert context["observability_id"] == OBSERVABILITY_ID


def test_a_submission_without_an_observability_id_gets_a_new_one(client_for):
    calculations = FakeHouseholdCalculations()

    with client_for(calculations) as client:
        result = client.post("/household-calculations", json=SUBMISSION)

    observability_id = result.headers[OBSERVABILITY_ID_HEADER]
    assert str(UUID(observability_id)) == observability_id
    assert calculations.submissions[0][1]["observability_id"] == observability_id


@pytest.mark.parametrize(
    "body",
    [
        {**SUBMISSION, "country": "fr"},
        {**SUBMISSION, "household_id": "not-a-uuid"},
        {**SUBMISSION, "year": 2026},
        {"country": "us"},
        {**SUBMISSION, "spm": None},
        {**SUBMISSION, "spm": "ce_trend"},
        {**SUBMISSION, "axes": [[{"name": "age", "count": 0, "min": 0, "max": 1}]]},
    ],
)
def test_a_malformed_submission_is_refused_with_its_reasons(client_for, body):
    calculations = FakeHouseholdCalculations()

    with client_for(calculations) as client:
        result = client.post("/household-calculations", json=body)

    assert result.status_code == 422
    assert all(
        {"loc", "msg", "type"} <= set(reason) for reason in result.json()["detail"]
    )
    assert calculations.submissions == []


@pytest.mark.parametrize(
    ("failure", "status_code", "body"),
    [
        (
            HouseholdCalculationRefusedError(
                code="unknown_household", detail="us has no household x"
            ),
            400,
            {"code": "unknown_household", "detail": "us has no household x"},
        ),
        (
            HouseholdCalculationUnprocessableError(
                (
                    UnprocessableReason(
                        loc=("body", "policy_id", "gov.removed"),
                        msg="the catalog version for .py 6.17.1 has no such parameter",
                        type="missing_parameter",
                    ),
                )
            ),
            422,
            {
                "detail": [
                    {
                        "loc": ["body", "policy_id", "gov.removed"],
                        "msg": (
                            "the catalog version for .py 6.17.1 has no such parameter"
                        ),
                        "type": "missing_parameter",
                    }
                ]
            },
        ),
        (
            HouseholdCalculationUnavailableError(
                code="database_unavailable",
                detail="The simulation database could not be read.",
            ),
            503,
            {
                "code": "database_unavailable",
                "detail": "The simulation database could not be read.",
            },
        ),
        (
            HouseholdCalculationInternalError(
                "the household function's request has a field the service "
                "built wrong: bundle"
            ),
            500,
            {
                "code": "internal_error",
                "detail": (
                    "The service could not build the household function's request."
                ),
            },
        ),
        (
            HouseholdCalculationUnavailableError(
                code="modal_call_refused",
                detail="Modal did not accept the call.",
                household_job_id=JOB_ID,
            ),
            503,
            {
                "code": "modal_call_refused",
                "detail": "Modal did not accept the call.",
                "household_job_id": str(JOB_ID),
            },
        ),
    ],
)
def test_a_refused_submission_answers_with_its_reason(
    client_for, failure, status_code, body
):
    with client_for(FakeHouseholdCalculations(failure=failure)) as client:
        result = client.post("/household-calculations", json=SUBMISSION)

    assert result.status_code == status_code
    assert result.json() == body
    assert (result.headers.get("Retry-After") == "10") is (status_code == 503)


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("post", "/household-calculations", SUBMISSION),
        ("get", STATE_PATH, None),
    ],
)
def test_without_a_database_or_v2_environment_the_routes_are_unavailable(
    client_for, method, path, body
):
    with client_for(None) as client:
        result = getattr(client, method)(path, **({"json": body} if body else {}))

    assert result.status_code == 503
    assert result.json() == {
        "code": "not_configured",
        "detail": "Household calculations are not configured.",
    }
    assert result.headers["Retry-After"] == "10"


def test_a_job_is_read_with_its_result_document(client_for):
    calculations = FakeHouseholdCalculations(stored=_record())

    with client_for(calculations) as client:
        result = client.get(STATE_PATH)

    assert result.status_code == 200
    assert result.json() == {
        "job_id": str(JOB_ID),
        "status": "succeeded",
        "country": "us",
        "household_id": str(HOUSEHOLD_ID),
        "policy_id": None,
        "year": 2026,
        "policyengine_version": "6.17.1",
        "country_package_version": "2.2.1",
        "result": RESULT_DOCUMENT,
        "error_message": None,
        "created_at": "2026-11-10T12:00:00Z",
        "started_at": "2026-11-10T12:00:05Z",
        "completed_at": "2026-11-10T12:00:30Z",
    }
    assert calculations.reads == [JOB_ID]


def test_a_failed_job_is_read_with_its_error_message(client_for):
    calculations = FakeHouseholdCalculations(
        stored=_record(
            status=HouseholdJobStatus.FAILED,
            result=None,
            error_message="Modal did not accept the household function's call.",
            started_at=None,
        )
    )

    with client_for(calculations) as client:
        result = client.get(STATE_PATH)

    assert result.status_code == 200
    assert result.json()["status"] == "failed"
    assert result.json()["result"] is None
    assert result.json()["error_message"] == (
        "Modal did not accept the household function's call."
    )


def test_reading_a_job_never_invents_an_observability_id(client_for):
    """household_jobs records none, so the read returns none, whatever is sent."""
    with client_for(FakeHouseholdCalculations(stored=_record())) as client:
        result = client.get(
            STATE_PATH, headers={OBSERVABILITY_ID_HEADER: OBSERVABILITY_ID}
        )

    assert OBSERVABILITY_ID_HEADER not in result.headers


def test_an_unknown_job_answers_404(client_for):
    with client_for(FakeHouseholdCalculations()) as client:
        result = client.get(STATE_PATH)

    assert result.status_code == 404
    assert result.json() == {
        "code": "unknown_household_job",
        "detail": f"There is no household job {JOB_ID}.",
    }


def test_a_job_that_cannot_be_read_answers_503(client_for):
    failure = HouseholdCalculationUnavailableError(
        code="database_unavailable",
        detail="The simulation database could not be read.",
    )

    with client_for(FakeHouseholdCalculations(failure=failure)) as client:
        result = client.get(STATE_PATH)

    assert result.status_code == 503
    assert result.json()["code"] == "database_unavailable"
    assert result.headers["Retry-After"] == "10"


def test_a_job_id_that_is_not_a_uuid_is_refused(client_for):
    calculations = FakeHouseholdCalculations()

    with client_for(calculations) as client:
        result = client.get("/household-calculations/not-a-uuid")

    assert result.status_code == 422
    assert calculations.reads == []


DATABASE_URL = "postgresql://simulation_service:secret@db.example:5432/postgres"


def test_a_configured_service_builds_household_calculations_without_connecting():
    """Nothing reaches the database or Modal until the first submission."""
    database = SimulationDatabase.from_url(DATABASE_URL)
    try:
        calculations = app_module.household_calculations_for(
            make_settings(
                simulation_database_url=DATABASE_URL, v2_modal_environment="staging"
            ),
            database,
        )
    finally:
        database.dispose()

    assert isinstance(calculations, HouseholdCalculationService)


@pytest.mark.parametrize(
    "settings_changes",
    [
        {},
        {"simulation_database_url": DATABASE_URL},
        {"v2_modal_environment": "staging"},
    ],
)
def test_without_its_database_and_v2_environment_none_is_built(settings_changes):
    database = SimulationDatabase.from_url(DATABASE_URL)
    try:
        calculations = app_module.household_calculations_for(
            make_settings(**settings_changes), database
        )
    finally:
        database.dispose()

    assert calculations is None


# What an unexpected failure answers, the same in the generated client's test.
UNEXPECTED_FAILURE_ANSWER = {
    "code": "internal_error",
    "detail": "The service could not answer the request.",
}


def _emitted_records(printed: str, message: str) -> list[dict[str, Any]]:
    """Return the records with ``message`` the observability runtime printed.

    The service's runtime writes each record to standard output as one JSON
    object per line of text.
    """
    records = [
        json.loads(printed_line)
        for printed_line in printed.splitlines()
        if printed_line.startswith("{")
    ]
    return [record for record in records if record.get("message") == message]


# Each route's handler, the outermost frame of an error it answers.
HANDLER_OF_METHOD = {
    "post": "submit_household_calculation",
    "get": "get_household_calculation",
}


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("post", "/household-calculations", SUBMISSION),
        ("get", STATE_PATH, None),
    ],
)
def test_an_unexpected_failure_answers_a_json_500_without_its_values(
    client_for, capsys, method, path, body
):
    """The record the runtime emits keeps the route, the type and the frames."""
    calculations = FakeHouseholdCalculations(
        failure=RuntimeError("a value that must not be logged")
    )

    with client_for(calculations) as client:
        result = getattr(client, method)(path, **({"json": body} if body else {}))

    assert result.status_code == 500
    assert result.headers["content-type"] == "application/json"
    assert result.json() == UNEXPECTED_FAILURE_ANSWER
    printed = capsys.readouterr().out
    (record,) = _emitted_records(printed, "household_route_failed")
    attributes = record["attributes"]
    assert attributes["error_type"] == "RuntimeError"
    assert attributes["route"] in {"/household-calculations", STATE_TEMPLATE}
    # Every attribute was kept, and every frame, innermost first: from where
    # the fake raised the error up to the route's handler.
    assert "attributes.omitted_count" not in record
    assert attributes["stack_frames_omitted"] == 0
    frames = attributes["stack_frames"].split("; ")
    assert "test_household_routes.py" in frames[0]
    assert frames[0].endswith(" in _raise_from_nested_calls")
    assert frames[1].endswith((" in submit", " in read"))
    assert frames[-1].endswith(f" in {HANDLER_OF_METHOD[method]}")
    assert not any(key.startswith("error.") for key in record)
    assert "a value that must not be logged" not in printed


# A string limit that holds a few frames only, and enough calls to pass it.
SMALL_STRING_LIMIT = 600
NESTED_CALLS = 20

# Where the service's image installs packages (its virtual environment is
# /opt/venv), and the files of a library and of the persistence library.
IMAGE_SITE_PACKAGES = "/opt/venv/lib/python3.13/site-packages"
LIBRARY_FILE = "sqlalchemy/engine/base.py"
PERSISTENCE_FILE = "policyengine_simulation_persistence/household_job_store.py"
# The library's connect raises the failure from ``depth`` nested calls of
# itself, on its third line; the persistence library calls it.
LIBRARY_SOURCE = """\
def connect(failure, depth):
    if depth == 0:
        raise failure
    connect(failure, depth - 1)
"""
LIBRARY_RAISE_LINE_NUMBER = 3
PERSISTENCE_SOURCE = """\
def read_household_job(failure, depth):
    connect(failure, depth)
"""


def _defined_in_file(file_path: str, source: str, **names: Any) -> dict[str, Any]:
    """Run ``source`` as code read from ``file_path``; return its globals.

    Each function it defines then has frames with that path, as an installed
    package's functions do. ``names`` are its globals before it runs.
    """
    namespace = dict(names)
    exec(compile(source, file_path, "exec"), namespace)
    return namespace


def test_an_unexpected_failure_leaves_out_library_frames_before_service_frames(
    client_for, capsys, monkeypatch
):
    """The service's frames are kept, and the library's outer frames counted.

    The error is raised deep in a library that the persistence library
    called, both at the image's paths. Each kept frame's path starts after
    ``site-packages/`` or the checkout's ``src/``. The string holds the
    frame that raised the error, the service's own frames, and as many of
    the library's inner frames as fit the limit, which comes from the
    runtime the service logs through.
    """
    build_runtime = app_module.init_simulation_observability

    def runtime_with_a_small_limit(*arguments, **keywords):
        runtime = build_runtime(*arguments, **keywords)
        runtime.config = dataclasses.replace(
            runtime.config,
            limits=TelemetryLimits(max_string_length=SMALL_STRING_LIMIT),
        )
        return runtime

    monkeypatch.setattr(
        app_module, "init_simulation_observability", runtime_with_a_small_limit
    )
    library = _defined_in_file(f"{IMAGE_SITE_PACKAGES}/{LIBRARY_FILE}", LIBRARY_SOURCE)
    persistence = _defined_in_file(
        f"{IMAGE_SITE_PACKAGES}/{PERSISTENCE_FILE}",
        PERSISTENCE_SOURCE,
        connect=library["connect"],
    )
    calculations = FakeHouseholdCalculations(
        failure=RuntimeError("a value that must not be logged"),
        failure_depth=NESTED_CALLS,
        raise_failure=persistence["read_household_job"],
    )

    with client_for(calculations) as client:
        result = client.post("/household-calculations", json=SUBMISSION)

    assert result.status_code == 500
    (record,) = _emitted_records(capsys.readouterr().out, "household_route_failed")
    attributes = record["attributes"]
    frames = attributes["stack_frames"].split("; ")
    assert len(attributes["stack_frames"]) <= SMALL_STRING_LIMIT
    # The service's frames come last, innermost first: the persistence
    # library's call, then the route's handler.
    assert [frame.rsplit(" in ", 1)[1] for frame in frames[-2:]] == [
        "read_household_job",
        "submit_household_calculation",
    ]
    assert frames[-2].startswith(f"{PERSISTENCE_FILE}:")
    assert frames[-1].startswith("policyengine_simulation_entry/household_routes.py:")
    # Before them, the library's innermost frames, from where it raised.
    library_frames = frames[:-2]
    assert library_frames[0] == (
        f"{LIBRARY_FILE}:{LIBRARY_RAISE_LINE_NUMBER} in connect"
    )
    assert all(frame.startswith(f"{LIBRARY_FILE}:") for frame in library_frames)
    assert 1 < len(library_frames) < NESTED_CALLS + 1
    # Left out: the library's outer frames and the fake's submit, of the
    # library's NESTED_CALLS + 1, the persistence call, submit and the handler.
    assert len(frames) + attributes["stack_frames_omitted"] == NESTED_CALLS + 4
