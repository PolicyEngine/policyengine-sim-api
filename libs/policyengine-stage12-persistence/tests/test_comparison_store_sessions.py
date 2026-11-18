"""The comparison store and the deployment check on ORM sessions.

They act as the comparison path's runtime role, which API v1's
manifest grants on the two temporary tables.
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Self
from uuid import UUID, uuid4

import pytest
from policyengine_simulation_contract.stage12_execution import (
    ComparisonReportRecord,
    ComparisonRunAggregationStatus,
    ComparisonRunLifecycleStatus,
    ComparisonSimulationRecord,
    ResultComparisonStatus,
    SimulationRole,
)
from policyengine_simulation_persistence import engine as persistence_engine
from policyengine_simulation_persistence.engine import (
    create_session_factory,
    dispose_session_factory,
)
from policyengine_stage12_persistence import (
    Stage12PersistenceStore,
    Stage12RuntimeAccessError,
    create_stage12_check_session_factory,
    validate_runtime_database,
    validation,
)
from policyengine_stage12_persistence.tables import (
    REPORT_CHECK_CONSTRAINTS,
    REPORT_IDENTITY_CONSTRAINT,
    REPORT_TABLE_NAME,
    ComparisonReport,
)
from policyengine_stage12_persistence.validation import (
    _validate_foreign_key,
    _validate_table_schema,
)
from report_run_database import SEED_URL_ENV, worker_database_url
from session_database import (
    RUNTIME_ROLE,
    StatementLog,
    act_as_role,
    limit_open_connections,
    record_opened_connections,
    refuse_connection_numbers,
)
from sqlalchemy import delete, event, func, inspect, make_url, select, true
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from collections.abc import Iterator

    from report_run_database import ReportRunSeeder
    from sqlalchemy.orm import sessionmaker

NOW = datetime(2026, 9, 22, tzinfo=UTC)
LATER = NOW + timedelta(minutes=5)
PLACEHOLDER_CALL = "dispatch-pending-1"
DIGEST = "a" * 64
# The connections a passing deployment check opens without a pool, one at a
# time: its role check, the schema inspector, eleven schema reads and its
# rolled-back writes.
CHECK_CONNECTIONS = 14


def _report(evaluation_id: UUID) -> ComparisonReportRecord:
    return ComparisonReportRecord(
        evaluation_id=evaluation_id,
        status=ComparisonRunLifecycleStatus.PENDING,
        aggregation_status=ComparisonRunAggregationStatus.NOT_STARTED,
        environment="staging",
        calculation_flow="economy",
        originating_request_id="request-1",
        observability_id="00000000-0000-4000-8000-000000000012",
        production_identity=f"job-{evaluation_id}",
        incumbent_execution_id="job-1",
        worker_version="5.2.0",
        modal_application="policyengine-simulation-v2-py5-2-0",
        report_coordinator_callable="coordinate_report",
        version_manifest_sha256=DIGEST,
        policyengine_version="5.2.0",
        country_package_name="policyengine-us",
        country_package_version="1.764.6",
        country="us",
        dataset_identity="populace_us_2024",
        dataset_uri="hf://policyengine/data/populace_us_2024.h5@revision",
        data_package_name="populace-data",
        data_package_version="0.1.0",
        data_artifact_revision="revision",
        created_at=NOW,
        updated_at=NOW,
        retention_expires_at=NOW + timedelta(days=30),
    )


def _simulation(evaluation_id: UUID) -> ComparisonSimulationRecord:
    return ComparisonSimulationRecord(
        simulation_execution_id=uuid4(),
        evaluation_id=evaluation_id,
        role=SimulationRole.BASELINE,
        input_sha256="b" * 64,
        worker_version="5.2.0",
        modal_application="policyengine-simulation-v2-py5-2-0",
        simulation_callable="run_single_simulation_us",
        version_manifest_sha256=DIGEST,
        modal_invocation_id=PLACEHOLDER_CALL,
        status=ComparisonRunLifecycleStatus.PENDING,
        created_at=NOW,
        updated_at=NOW,
        retention_expires_at=NOW + timedelta(days=30),
    )


@pytest.fixture
def statements(runtime_sessions: sessionmaker[Session]) -> Iterator[StatementLog]:
    log = StatementLog(runtime_sessions)
    yield log
    log.assert_every_statement_ran_on_a_session()


@pytest.fixture
def store(
    runtime_sessions: sessionmaker[Session], statements: StatementLog
) -> Stage12PersistenceStore:
    assert statements.sent == []
    return Stage12PersistenceStore(session_factory=runtime_sessions)


def _deployment_command_sessions() -> sessionmaker[Session]:
    """Return the deployment command's session factory, as the runtime role."""
    worker_database_url()
    database_url = make_url(os.environ[SEED_URL_ENV]).set(drivername="postgresql")
    session_factory = create_stage12_check_session_factory(
        database_url.render_as_string(hide_password=False)
    )
    act_as_role(session_factory.kw["bind"], RUNTIME_ROLE)
    return session_factory


@pytest.fixture
def check_sessions() -> Iterator[sessionmaker[Session]]:
    """Yield the deployment command's session factory, as the runtime role."""
    session_factory = _deployment_command_sessions()
    try:
        yield session_factory
    finally:
        dispose_session_factory(session_factory)


@pytest.fixture
def retry_waits(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record the deployment check's waits between attempts instead of waiting."""
    waits: list[float] = []
    monkeypatch.setattr(time, "sleep", waits.append)
    return waits


@pytest.fixture
def evaluation_id(seeder: ReportRunSeeder) -> Iterator[UUID]:
    """Yield a report's ID; its row and simulations are removed after the test."""
    new_id = uuid4()
    yield new_id
    with Session(seeder.engine) as session:
        session.execute(
            delete(ComparisonReport).where(ComparisonReport.evaluation_id == new_id)
        )
        session.commit()


def test_a_report_is_created_once_and_read_back(store, evaluation_id) -> None:
    report = _report(evaluation_id)

    created = store.create_or_resolve_report(report)
    resolved = store.create_or_resolve_report(report)

    assert created.created is True
    assert created.record == report
    assert resolved.created is False
    assert resolved.record == report
    assert store.get_report(evaluation_id) == report
    with pytest.raises(LookupError, match="does not exist"):
        store.get_report(uuid4())


def test_a_report_with_the_same_identity_and_other_provenance_is_refused(
    store, evaluation_id
) -> None:
    report = _report(evaluation_id)
    store.create_or_resolve_report(report)
    conflicting = report.model_copy(
        update={"evaluation_id": uuid4(), "country_package_version": "9.9.9"}
    )

    with pytest.raises(ValueError, match="country_package_version"):
        store.create_or_resolve_report(conflicting)


def test_a_reports_lifecycle_is_replaced_under_its_row_lock(
    store, statements, evaluation_id
) -> None:
    report = _report(evaluation_id)
    store.create_or_resolve_report(report)
    running = report.model_copy(
        update={
            "status": ComparisonRunLifecycleStatus.RUNNING,
            "started_at": LATER,
            "updated_at": LATER,
        }
    )

    replaced = store.replace_report(running)

    assert replaced == running
    assert store.get_report(evaluation_id) == running
    assert [sent for sent in statements.sent if sent.endswith("FOR UPDATE")]


def test_a_reports_result_comparison_is_replaced_once_final(
    store, evaluation_id
) -> None:
    report = _report(evaluation_id).model_copy(
        update={"comparison_status": ResultComparisonStatus.PENDING}
    )
    store.create_or_resolve_report(report)
    matched = report.model_copy(
        update={
            "comparison_status": ResultComparisonStatus.MATCHED,
            "comparison_output_uri": "gs://bucket/comparison",
            "comparison_output_sha256": DIGEST,
            "comparison_schema_version": 1,
            "comparison_completed_at": LATER,
            "updated_at": LATER,
        }
    )

    first = store.replace_report_result_comparison(matched)
    replayed = store.replace_report_result_comparison(matched)

    assert first == matched
    assert replayed == matched


def test_a_simulation_needs_its_report_and_is_created_once(
    store, evaluation_id
) -> None:
    simulation = _simulation(evaluation_id)
    with pytest.raises(LookupError, match="does not exist"):
        store.create_or_resolve_simulation(simulation)
    store.create_or_resolve_report(_report(evaluation_id))

    created = store.create_or_resolve_simulation(simulation)
    resolved = store.create_or_resolve_simulation(simulation)

    assert created.created is True
    assert created.record == simulation
    assert resolved.created is False
    assert store.get_simulation(simulation.simulation_execution_id) == simulation
    assert store.list_simulations(evaluation_id) == (simulation,)


def test_a_simulation_is_replaced_only_in_its_expected_status(
    store, evaluation_id
) -> None:
    store.create_or_resolve_report(_report(evaluation_id))
    simulation = _simulation(evaluation_id)
    store.create_or_resolve_simulation(simulation)
    running = simulation.model_copy(
        update={
            "status": ComparisonRunLifecycleStatus.RUNNING,
            "started_at": LATER,
            "updated_at": LATER,
        }
    )
    assert store.replace_simulation(running) == running
    failed = running.model_copy(
        update={
            "status": ComparisonRunLifecycleStatus.FAILED,
            "error_code": "simulation_execution_failed",
            "completed_at": LATER,
        }
    )

    stale, stale_replaced = store.replace_simulation_if_status(
        failed, expected_status=ComparisonRunLifecycleStatus.PENDING
    )
    current, replaced = store.replace_simulation_if_status(
        failed, expected_status=ComparisonRunLifecycleStatus.RUNNING
    )

    assert (stale, stale_replaced) == (running, False)
    assert (current, replaced) == (failed, True)


def test_a_simulations_invocation_replaces_only_the_placeholder(
    store, evaluation_id
) -> None:
    store.create_or_resolve_report(_report(evaluation_id))
    simulation = _simulation(evaluation_id)
    store.create_or_resolve_simulation(simulation)
    simulation_id = simulation.simulation_execution_id

    def attach(call: str) -> ComparisonSimulationRecord:
        return store.attach_simulation_invocation(
            simulation_id,
            expected_placeholder=PLACEHOLDER_CALL,
            modal_invocation_id=call,
            updated_at=LATER,
        )

    attached = attach("fc-123")
    repeated = attach("fc-123")

    assert attached.modal_invocation_id == "fc-123"
    assert repeated == attached
    with pytest.raises(ValueError, match="invocation identity changed"):
        attach("fc-other")
    with pytest.raises(LookupError, match="does not exist"):
        store.attach_simulation_invocation(
            uuid4(),
            expected_placeholder=PLACEHOLDER_CALL,
            modal_invocation_id="fc-123",
            updated_at=LATER,
        )


def test_the_deployment_check_passes_and_leaves_no_row(
    runtime_sessions, seeder
) -> None:
    # The schema inspection reads through the engine, outside any session.
    statements = StatementLog(runtime_sessions)

    def comparison_rows() -> int:
        with Session(seeder.engine) as session:
            return session.execute(
                select(func.count()).select_from(ComparisonReport)
            ).scalar_one()

    before = comparison_rows()

    validate_runtime_database(
        runtime_sessions, expected_role=RUNTIME_ROLE, environment="staging"
    )

    assert statements.starting_with("INSERT INTO public.stage12_evaluation_reports")
    assert statements.starting_with("DELETE FROM public.stage12_evaluation_reports")
    assert statements.commits == 0
    assert comparison_rows() == before


def _comparison_report_count(seeder: ReportRunSeeder) -> int:
    with Session(seeder.engine) as session:
        return session.execute(
            select(func.count()).select_from(ComparisonReport)
        ).scalar_one()


def test_the_deployment_command_runs_its_check_on_one_real_connection(
    check_sessions, retry_waits
) -> None:
    opened = record_opened_connections(check_sessions)

    validate_runtime_database(
        check_sessions, expected_role=RUNTIME_ROLE, environment="staging"
    )

    assert len(opened) == 1
    assert retry_waits == []


def test_the_deployment_command_replaces_a_connection_it_loses(
    check_sessions, seeder, retry_waits
) -> None:
    opened = record_opened_connections(check_sessions)
    reports_before = _comparison_report_count(seeder)
    terminated_backends: list[int] = []

    def terminate_during_the_first_insert(
        connection, _cursor, statement, _parameters, _context, _many
    ) -> None:
        if terminated_backends or not statement.startswith(
            f"INSERT INTO public.{REPORT_TABLE_NAME}"
        ):
            return
        backend = connection.connection.dbapi_connection.info.backend_pid
        terminated_backends.append(backend)
        with seeder.engine.begin() as administrator:
            administrator.execute(select(func.pg_terminate_backend(backend)))

    event.listen(
        check_sessions.kw["bind"],
        "before_cursor_execute",
        terminate_during_the_first_insert,
    )

    validate_runtime_database(
        check_sessions, expected_role=RUNTIME_ROLE, environment="staging"
    )

    assert len(terminated_backends) == 1
    assert len(opened) == 2
    assert retry_waits == [1.0]
    assert _comparison_report_count(seeder) == reports_before


def test_the_deployment_command_replaces_a_connection_lost_while_idle(
    check_sessions, seeder, retry_waits
) -> None:
    # The connection is checked before reuse, so no step fails or waits.
    opened = record_opened_connections(check_sessions)
    terminated_backends: list[int] = []

    def terminate_the_first_returned_connection(
        dbapi_connection, _connection_record
    ) -> None:
        if terminated_backends:
            return
        backend = dbapi_connection.info.backend_pid
        terminated_backends.append(backend)
        with seeder.engine.begin() as administrator:
            administrator.execute(select(func.pg_terminate_backend(backend)))

    event.listen(
        check_sessions.kw["bind"], "checkin", terminate_the_first_returned_connection
    )

    validate_runtime_database(
        check_sessions, expected_role=RUNTIME_ROLE, environment="staging"
    )

    assert len(terminated_backends) == 1
    assert len(opened) == 2
    assert retry_waits == []


def test_the_deployment_check_reports_a_connection_another_step_kept(
    retry_waits, monkeypatch
) -> None:
    monkeypatch.setattr(persistence_engine, "SINGLE_CONNECTION_WAIT_SECONDS", 0.05)
    session_factory = _deployment_command_sessions()
    try:
        with session_factory() as holder:
            holder.execute(select(true())).scalar_one()
            with pytest.raises(Stage12RuntimeAccessError, match="TimeoutError"):
                validate_runtime_database(
                    session_factory,
                    expected_role=RUNTIME_ROLE,
                    environment="staging",
                )
    finally:
        dispose_session_factory(session_factory)

    assert retry_waits == []


def test_the_deployment_check_needs_one_free_pooler_slot(runtime_sessions) -> None:
    # The deployment URL names a session pooler with a fixed client limit, so
    # the check never holds one connection while it opens another.
    limit_open_connections(runtime_sessions, limit=1)

    validate_runtime_database(
        runtime_sessions, expected_role=RUNTIME_ROLE, environment="staging"
    )


@pytest.mark.parametrize("refused_connection", range(1, CHECK_CONNECTIONS + 1))
def test_the_deployment_check_survives_any_one_refused_connection(
    runtime_sessions, retry_waits, refused_connection
) -> None:
    # A session pooler refuses clients while it is full, which says nothing
    # about the schema or the role. Every step of the check may be repeated.
    attempts = refuse_connection_numbers(runtime_sessions, {refused_connection})

    validate_runtime_database(
        runtime_sessions, expected_role=RUNTIME_ROLE, environment="staging"
    )

    assert len(attempts) > CHECK_CONNECTIONS
    assert retry_waits == [1.0]


def test_the_deployment_check_stops_when_connections_stay_refused(
    runtime_sessions, retry_waits
) -> None:
    attempts = refuse_connection_numbers(runtime_sessions, range(1, 1000))

    with pytest.raises(Stage12RuntimeAccessError, match="OperationalError"):
        validate_runtime_database(
            runtime_sessions, expected_role=RUNTIME_ROLE, environment="staging"
        )

    assert len(attempts) == 5
    assert retry_waits == [1.0, 2.0, 4.0, 8.0]


def test_the_deployment_check_refuses_another_role(
    runtime_sessions, retry_waits
) -> None:
    attempts = refuse_connection_numbers(runtime_sessions, ())

    with pytest.raises(Stage12RuntimeAccessError, match="unexpected role"):
        validate_runtime_database(
            runtime_sessions, expected_role="another_role", environment="staging"
        )

    assert len(attempts) == 1
    assert retry_waits == []


def test_the_deployment_check_refuses_an_unknown_environment(
    runtime_sessions,
) -> None:
    with pytest.raises(Stage12RuntimeAccessError, match="staging or production"):
        validate_runtime_database(
            runtime_sessions, expected_role=RUNTIME_ROLE, environment="local"
        )


class FailingSessions:
    """A session factory whose sessions fail every statement with one error."""

    def __init__(self, error: Exception) -> None:
        self.error = error

    def __call__(self) -> Self:
        return self

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exception_details) -> None:
        return None

    def rollback(self) -> None:
        return None

    def execute(self, _statement: object) -> None:
        raise self.error


@pytest.mark.parametrize(
    ("error_type", "retries"),
    [
        pytest.param(OperationalError, 4, id="connection-error-is-retried"),
        pytest.param(ProgrammingError, 0, id="other-database-error-is-not"),
    ],
)
def test_the_deployment_check_reports_a_database_error_without_its_text(
    retry_waits, error_type, retries
) -> None:
    sessions = FailingSessions(error_type(None, None, Exception("password=hidden")))

    with pytest.raises(Stage12RuntimeAccessError) as failure:
        validate_runtime_database(
            sessions,  # type: ignore[arg-type]
            expected_role=RUNTIME_ROLE,
            environment="staging",
        )

    assert error_type.__name__ in str(failure.value)
    assert "password" not in str(failure.value)
    assert len(retry_waits) == retries


def test_the_deployment_check_refuses_a_role_missing_a_table_privilege(
    worker_sessions, retry_waits
) -> None:
    with pytest.raises(
        Stage12RuntimeAccessError,
        match=f"lacks SELECT on {REPORT_TABLE_NAME}",
    ):
        validate_runtime_database(
            worker_sessions,
            expected_role="policyengine_v2_modal_worker",
            environment="staging",
        )

    assert retry_waits == []


def test_the_deployment_check_refuses_a_role_that_may_change_the_schema(
    seeder, retry_waits
) -> None:
    with pytest.raises(Stage12RuntimeAccessError, match="public-schema CREATE"):
        validate_runtime_database(
            create_session_factory(seeder.engine),
            expected_role="postgres",
            environment="staging",
        )

    assert retry_waits == []


def test_the_deployment_check_refuses_a_forbidden_table_privilege(
    runtime_sessions, retry_waits, monkeypatch
) -> None:
    has_privilege = validation._has_table_privilege

    def has_truncate_too(session: Session, table_name: str, privilege: str) -> bool:
        return privilege == "TRUNCATE" or has_privilege(session, table_name, privilege)

    monkeypatch.setattr(validation, "_has_table_privilege", has_truncate_too)

    with pytest.raises(Stage12RuntimeAccessError, match="unexpectedly has TRUNCATE"):
        validate_runtime_database(
            runtime_sessions, expected_role=RUNTIME_ROLE, environment="staging"
        )

    assert retry_waits == []


class AlteredSchema:
    """A live schema with one inspector answer replaced."""

    def __init__(self, inspector: Any, method_name: str, answer: Any) -> None:
        self._inspector = inspector
        self._method_name = method_name
        self._answer = answer

    def __getattr__(self, name: str) -> Any:
        if name == self._method_name:
            return lambda *_args, **_keywords: self._answer(
                getattr(self._inspector, name)(*_args, **_keywords)
            )
        return getattr(self._inspector, name)


def _without_first_column(columns: list[dict]) -> list[dict]:
    return columns[1:]


def _with_changed_nullability(columns: list[dict]) -> list[dict]:
    first = {**columns[0], "nullable": not columns[0]["nullable"]}
    return [first, *columns[1:]]


def _no_columns_constrained(constraint: dict) -> dict:
    return {**constraint, "constrained_columns": []}


def _nothing(_answer: object) -> list:
    return []


def _keys_to_another_table(foreign_keys: list[dict]) -> list[dict]:
    return [{**key, "referred_table": "another_table"} for key in foreign_keys]


@pytest.mark.parametrize(
    ("method_name", "answer", "message"),
    [
        pytest.param(
            "get_columns", _without_first_column, "columns do not match", id="columns"
        ),
        pytest.param(
            "get_columns",
            _with_changed_nullability,
            "unexpected nullability",
            id="nullability",
        ),
        pytest.param(
            "get_pk_constraint",
            _no_columns_constrained,
            "unexpected primary key",
            id="primary-key",
        ),
        pytest.param(
            "get_unique_constraints",
            _nothing,
            "unexpected identity constraint",
            id="identity",
        ),
        pytest.param(
            "get_check_constraints",
            _nothing,
            "missing required check constraints",
            id="checks",
        ),
        pytest.param("get_indexes", _nothing, "is missing the expected", id="indexes"),
    ],
)
def test_the_schema_check_compares_the_live_table_with_its_model(
    seeder, method_name, answer, message
) -> None:
    live_schema = inspect(seeder.engine)
    _validate_table_schema(
        live_schema,
        model=ComparisonReport,
        table_name=REPORT_TABLE_NAME,
        identity_constraint=REPORT_IDENTITY_CONSTRAINT,
        check_constraints=REPORT_CHECK_CONSTRAINTS,
    )

    with pytest.raises(Stage12RuntimeAccessError, match=message):
        _validate_table_schema(
            AlteredSchema(inspect(seeder.engine), method_name, answer),
            model=ComparisonReport,
            table_name=REPORT_TABLE_NAME,
            identity_constraint=REPORT_IDENTITY_CONSTRAINT,
            check_constraints=REPORT_CHECK_CONSTRAINTS,
        )


@pytest.mark.parametrize(
    ("answer", "message"),
    [
        pytest.param(_nothing, "foreign key is missing", id="missing"),
        pytest.param(
            _keys_to_another_table, "unexpected definition", id="another-table"
        ),
    ],
)
def test_the_schema_check_compares_the_simulations_foreign_key(
    seeder, answer, message
) -> None:
    _validate_foreign_key(inspect(seeder.engine))

    with pytest.raises(Stage12RuntimeAccessError, match=message):
        _validate_foreign_key(
            AlteredSchema(inspect(seeder.engine), "get_foreign_keys", answer)
        )
