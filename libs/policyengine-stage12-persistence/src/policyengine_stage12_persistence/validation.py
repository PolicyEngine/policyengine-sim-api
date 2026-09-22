"""Live, DML-only qualification of the API-owned Stage 12 schema."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import (
    JSON,
    DateTime,
    Integer,
    String,
    delete,
    func,
    inspect,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import ENUM, UUID, insert
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

from policyengine_stage12_persistence.tables import (
    REPORT_CHECK_CONSTRAINTS,
    REPORT_IDENTITY_CONSTRAINT,
    REPORT_TABLE_NAME,
    SIMULATION_CHECK_CONSTRAINTS,
    SIMULATION_IDENTITY_CONSTRAINT,
    SIMULATION_TABLE_NAME,
    comparison_reports,
    comparison_simulations,
)

REQUIRED_TABLE_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE")
FORBIDDEN_TABLE_PRIVILEGES = ("TRUNCATE", "REFERENCES", "TRIGGER")


class Stage12RuntimeAccessError(RuntimeError):
    """The deployed schema or shared runtime credential is not usable safely."""


def _require_column_type(expected: object, actual: object, *, column: str) -> None:
    if isinstance(expected, ENUM):
        if not isinstance(actual, ENUM):
            raise Stage12RuntimeAccessError(f"{column} has an unexpected type")
        if expected.name != actual.name or tuple(expected.enums) != tuple(actual.enums):
            raise Stage12RuntimeAccessError(f"{column} has an unexpected enum")
        return
    if isinstance(expected, UUID):
        if not isinstance(actual, UUID):
            raise Stage12RuntimeAccessError(f"{column} has an unexpected type")
        return
    if isinstance(expected, JSON):
        if not isinstance(actual, JSON):
            raise Stage12RuntimeAccessError(f"{column} has an unexpected type")
        return
    if isinstance(expected, DateTime):
        if not isinstance(actual, DateTime) or not actual.timezone:
            raise Stage12RuntimeAccessError(f"{column} has an unexpected type")
        return
    if isinstance(expected, String):
        if not isinstance(actual, String) or expected.length != actual.length:
            raise Stage12RuntimeAccessError(f"{column} has an unexpected type")
        return
    if isinstance(expected, Integer) and not isinstance(actual, Integer):
        raise Stage12RuntimeAccessError(f"{column} has an unexpected type")


def _validate_table_schema(
    connection: Connection,
    *,
    table: object,
    table_name: str,
    identity_constraint: str,
    check_constraints: frozenset[str],
) -> None:
    inspector = inspect(connection)
    live_columns = {
        item["name"]: item
        for item in inspector.get_columns(table_name, schema="public")
    }
    declared_columns = list(table.c)  # type: ignore[attr-defined]
    if set(live_columns) != {item.name for item in declared_columns}:
        raise Stage12RuntimeAccessError(
            f"{table_name} columns do not match the synchronized mapping"
        )
    for declared in declared_columns:
        live = live_columns[declared.name]
        if declared.nullable != live["nullable"]:
            raise Stage12RuntimeAccessError(
                f"{table_name}.{declared.name} has unexpected nullability"
            )
        _require_column_type(
            declared.type,
            live["type"],
            column=f"{table_name}.{declared.name}",
        )

    primary_key = inspector.get_pk_constraint(table_name, schema="public")
    declared_primary_key = [column.name for column in table.primary_key]  # type: ignore[attr-defined]
    if primary_key.get("constrained_columns") != declared_primary_key:
        raise Stage12RuntimeAccessError(f"{table_name} has an unexpected primary key")

    unique_constraints = {
        item.get("name"): tuple(item.get("column_names") or ())
        for item in inspector.get_unique_constraints(table_name, schema="public")
    }
    declared_unique = next(
        constraint
        for constraint in table.constraints  # type: ignore[attr-defined]
        if constraint.name == identity_constraint
    )
    if unique_constraints.get(identity_constraint) != tuple(
        column.name for column in declared_unique.columns
    ):
        raise Stage12RuntimeAccessError(
            f"{table_name} has an unexpected identity constraint"
        )

    live_checks = {
        item.get("name")
        for item in inspector.get_check_constraints(table_name, schema="public")
    }
    if not check_constraints.issubset(live_checks):
        raise Stage12RuntimeAccessError(
            f"{table_name} is missing required check constraints"
        )

    live_indexes = {
        item.get("name"): tuple(item.get("column_names") or ())
        for item in inspector.get_indexes(table_name, schema="public")
    }
    declared_indexes = {
        index.name: tuple(column.name for column in index.columns)
        for index in table.indexes  # type: ignore[attr-defined]
    }
    for name, columns in declared_indexes.items():
        if live_indexes.get(name) != columns:
            raise Stage12RuntimeAccessError(
                f"{table_name} is missing the expected {name} index"
            )


def _validate_foreign_key(connection: Connection) -> None:
    foreign_keys = inspect(connection).get_foreign_keys(
        SIMULATION_TABLE_NAME,
        schema="public",
    )
    expected = next(
        (
            item
            for item in foreign_keys
            if item.get("constrained_columns") == ["evaluation_id"]
        ),
        None,
    )
    if expected is None:
        raise Stage12RuntimeAccessError("simulation parent foreign key is missing")
    if (
        expected.get("referred_schema") not in {None, "public"}
        or expected.get("referred_table") != REPORT_TABLE_NAME
        or expected.get("referred_columns") != ["evaluation_id"]
        or (expected.get("options") or {}).get("ondelete") != "CASCADE"
    ):
        raise Stage12RuntimeAccessError(
            "simulation parent foreign key has an unexpected definition"
        )


def _has_schema_privilege(connection: Connection, privilege: str) -> bool:
    return bool(
        connection.execute(
            select(func.has_schema_privilege("public", privilege))
        ).scalar_one()
    )


def _has_table_privilege(
    connection: Connection,
    table_name: str,
    privilege: str,
) -> bool:
    return bool(
        connection.execute(
            select(
                func.has_table_privilege(
                    f"public.{table_name}",
                    privilege,
                )
            )
        ).scalar_one()
    )


def _validate_permissions(connection: Connection, *, expected_role: str) -> None:
    current_role = connection.execute(select(func.current_user())).scalar_one()
    if current_role != expected_role:
        raise Stage12RuntimeAccessError(
            "Stage 12 credential authenticated as an unexpected role"
        )
    if not _has_schema_privilege(connection, "USAGE"):
        raise Stage12RuntimeAccessError("runtime role lacks public-schema USAGE")
    if _has_schema_privilege(connection, "CREATE"):
        raise Stage12RuntimeAccessError("runtime role has public-schema CREATE")
    for table_name in (REPORT_TABLE_NAME, SIMULATION_TABLE_NAME):
        for privilege in REQUIRED_TABLE_PRIVILEGES:
            if not _has_table_privilege(connection, table_name, privilege):
                raise Stage12RuntimeAccessError(
                    f"runtime role lacks {privilege} on {table_name}"
                )
        for privilege in FORBIDDEN_TABLE_PRIVILEGES:
            if _has_table_privilege(connection, table_name, privilege):
                raise Stage12RuntimeAccessError(
                    f"runtime role unexpectedly has {privilege} on {table_name}"
                )


def _exercise_runtime_dml(connection: Connection, *, environment: str) -> None:
    evaluation_id = uuid4()
    simulation_execution_id = uuid4()
    now = datetime.now(UTC)
    retention_expires_at = now + timedelta(days=1)
    digest = "0" * 64
    report_values = {
        "evaluation_id": evaluation_id,
        "contract_version": 1,
        "status": "pending",
        "aggregation_status": "not_started",
        "environment": environment,
        "calculation_flow": "infrastructure_validation",
        "originating_request_id": f"validation-{evaluation_id}",
        "production_identity": f"validation-{evaluation_id}",
        "incumbent_execution_id": None,
        "worker_version": "validation",
        "modal_application": "stage12-infrastructure-validation",
        "report_coordinator_callable": "coordinate_report",
        "version_manifest_sha256": digest,
        "policyengine_version": "validation",
        "country_package_name": "policyengine-validation",
        "country_package_version": "validation",
        "country": "us",
        "dataset_identity": "validation",
        "dataset_uri": "hf://validation/dataset@revision",
        "data_package_name": "validation-data",
        "data_package_version": "validation",
        "data_artifact_revision": "revision",
        "coordinator_invocation_id": None,
        "error_code": None,
        "error_summary": None,
        "aggregate_output_uri": None,
        "aggregate_output_sha256": None,
        "aggregate_schema_version": None,
        "comparison_status": "pending",
        "comparison_output_uri": None,
        "comparison_output_sha256": None,
        "comparison_schema_version": None,
        "comparison_completed_at": None,
        "comparison_error_code": None,
        "comparison_error_summary": None,
        "created_at": now,
        "updated_at": now,
        "started_at": None,
        "completed_at": None,
        "retention_expires_at": retention_expires_at,
    }
    inserted_report = connection.execute(
        insert(comparison_reports)
        .values(**report_values)
        .returning(comparison_reports.c.evaluation_id)
    ).scalar_one()
    if inserted_report != evaluation_id:
        raise Stage12RuntimeAccessError("report DML canary insert failed")
    selected_report = connection.execute(
        select(comparison_reports.c.evaluation_id).where(
            comparison_reports.c.evaluation_id == evaluation_id
        )
    ).scalar_one()
    if selected_report != evaluation_id:
        raise Stage12RuntimeAccessError("report DML canary select failed")
    updated_report = connection.execute(
        update(comparison_reports)
        .where(comparison_reports.c.evaluation_id == evaluation_id)
        .values(updated_at=datetime.now(UTC))
        .returning(comparison_reports.c.evaluation_id)
    ).scalar_one()
    if updated_report != evaluation_id:
        raise Stage12RuntimeAccessError("report DML canary update failed")

    simulation_values = {
        "simulation_execution_id": simulation_execution_id,
        "evaluation_id": evaluation_id,
        "contract_version": 1,
        "role": "baseline",
        "input_sha256": digest,
        "worker_version": "validation",
        "modal_application": "stage12-infrastructure-validation",
        "simulation_callable": "run_single_simulation_us",
        "version_manifest_sha256": digest,
        "modal_invocation_id": None,
        "status": "pending",
        "error_code": None,
        "error_summary": None,
        "output_uri": None,
        "output_sha256": None,
        "output_schema_version": None,
        "row_identity_columns": None,
        "row_count": None,
        "row_identity_sha256": None,
        "created_at": now,
        "updated_at": now,
        "started_at": None,
        "completed_at": None,
        "retention_expires_at": retention_expires_at,
    }
    inserted_simulation = connection.execute(
        insert(comparison_simulations)
        .values(**simulation_values)
        .returning(comparison_simulations.c.simulation_execution_id)
    ).scalar_one()
    if inserted_simulation != simulation_execution_id:
        raise Stage12RuntimeAccessError("simulation DML canary insert failed")
    selected_simulation = connection.execute(
        select(comparison_simulations.c.simulation_execution_id).where(
            comparison_simulations.c.simulation_execution_id == simulation_execution_id
        )
    ).scalar_one()
    if selected_simulation != simulation_execution_id:
        raise Stage12RuntimeAccessError("simulation DML canary select failed")
    updated_simulation = connection.execute(
        update(comparison_simulations)
        .where(
            comparison_simulations.c.simulation_execution_id == simulation_execution_id
        )
        .values(updated_at=datetime.now(UTC))
        .returning(comparison_simulations.c.simulation_execution_id)
    ).scalar_one()
    if updated_simulation != simulation_execution_id:
        raise Stage12RuntimeAccessError("simulation DML canary update failed")
    deleted_simulation = connection.execute(
        delete(comparison_simulations)
        .where(
            comparison_simulations.c.simulation_execution_id == simulation_execution_id
        )
        .returning(comparison_simulations.c.simulation_execution_id)
    ).scalar_one()
    if deleted_simulation != simulation_execution_id:
        raise Stage12RuntimeAccessError("simulation DML canary delete failed")
    deleted_report = connection.execute(
        delete(comparison_reports)
        .where(comparison_reports.c.evaluation_id == evaluation_id)
        .returning(comparison_reports.c.evaluation_id)
    ).scalar_one()
    if deleted_report != evaluation_id:
        raise Stage12RuntimeAccessError("report DML canary delete failed")


def validate_runtime_database(
    engine: Engine,
    *,
    expected_role: str,
    environment: str,
) -> None:
    """Inspect the live schema and roll back a complete typed DML canary."""

    if environment not in {"staging", "production"}:
        raise Stage12RuntimeAccessError("environment must be staging or production")
    try:
        with engine.connect() as connection:
            _validate_permissions(connection, expected_role=expected_role)
            _validate_table_schema(
                connection,
                table=comparison_reports,
                table_name=REPORT_TABLE_NAME,
                identity_constraint=REPORT_IDENTITY_CONSTRAINT,
                check_constraints=REPORT_CHECK_CONSTRAINTS,
            )
            _validate_table_schema(
                connection,
                table=comparison_simulations,
                table_name=SIMULATION_TABLE_NAME,
                identity_constraint=SIMULATION_IDENTITY_CONSTRAINT,
                check_constraints=SIMULATION_CHECK_CONSTRAINTS,
            )
            _validate_foreign_key(connection)
            connection.rollback()
            transaction = connection.begin()
            try:
                _exercise_runtime_dml(connection, environment=environment)
            finally:
                transaction.rollback()
    except Stage12RuntimeAccessError:
        raise
    except SQLAlchemyError as error:
        raise Stage12RuntimeAccessError(
            f"Stage 12 runtime database verification failed ({type(error).__name__})"
        ) from None
