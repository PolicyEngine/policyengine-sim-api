"""Live verification for the restricted Stage 12 PostgreSQL credential."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg

from policyengine_simulation_contract.stage12_persistence import (
    REPORT_TABLE,
    SIMULATION_TABLE,
)

EXPECTED_ROLE_ATTRIBUTES = (False, False, False, False, False, False, True)
REQUIRED_TABLE_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE")
FORBIDDEN_TABLE_PRIVILEGES = ("TRUNCATE", "REFERENCES", "TRIGGER")


class Stage12RuntimeAccessError(RuntimeError):
    """Raised when the deployed runtime credential has unexpected access."""


def _require_one_row(cursor: Any, operation: str) -> None:
    if cursor.rowcount != 1:
        raise Stage12RuntimeAccessError(
            f"Stage 12 runtime {operation} did not affect exactly one canary row"
        )


def _exercise_runtime_dml(cursor: Any, *, environment: str) -> None:
    """Exercise the role's required table operations inside one transaction."""

    evaluation_id = uuid4()
    simulation_execution_id = uuid4()
    now = datetime.now(timezone.utc)
    retention_expires_at = now + timedelta(days=1)
    digest = "0" * 64

    cursor.execute(
        f"""
        INSERT INTO {REPORT_TABLE} (
            evaluation_id, contract_version, status, aggregation_status,
            environment, calculation_flow, originating_request_id,
            production_identity, worker_version, modal_application,
            report_coordinator_callable, version_manifest_sha256,
            policyengine_version, country_package_name,
            country_package_version, country, dataset_identity, dataset_uri,
            data_package_name, data_package_version, data_artifact_revision,
            created_at, updated_at, retention_expires_at
        ) VALUES (
            %s, 1, 'pending', 'not_started', %s, 'infrastructure_validation',
            %s, %s, 'validation', 'stage12-infrastructure-validation',
            'coordinate_report', %s, 'validation', 'policyengine-validation',
            'validation', 'us', 'validation', 'hf://validation/dataset@revision',
            'validation-data', 'validation', 'revision', %s, %s, %s
        )
        """,
        (
            evaluation_id,
            environment,
            f"validation-{evaluation_id}",
            f"validation-{evaluation_id}",
            digest,
            now,
            now,
            retention_expires_at,
        ),
    )
    _require_one_row(cursor, "report INSERT")
    cursor.execute(
        f"SELECT evaluation_id FROM {REPORT_TABLE} WHERE evaluation_id = %s",
        (evaluation_id,),
    )
    if cursor.fetchone() != (evaluation_id,):
        raise Stage12RuntimeAccessError(
            "Stage 12 runtime could not SELECT its canary report"
        )
    cursor.execute(
        f"""
        UPDATE {REPORT_TABLE}
        SET updated_at = %s
        WHERE evaluation_id = %s
        RETURNING evaluation_id
        """,
        (datetime.now(timezone.utc), evaluation_id),
    )
    if cursor.fetchone() != (evaluation_id,):
        raise Stage12RuntimeAccessError(
            "Stage 12 runtime could not UPDATE its canary report"
        )

    cursor.execute(
        f"""
        INSERT INTO {SIMULATION_TABLE} (
            simulation_execution_id, evaluation_id, contract_version, role,
            input_sha256, worker_version, modal_application,
            simulation_callable, version_manifest_sha256, status,
            created_at, updated_at, retention_expires_at
        ) VALUES (
            %s, %s, 1, 'baseline', %s, 'validation',
            'stage12-infrastructure-validation', 'run_single_simulation_us',
            %s, 'pending', %s, %s, %s
        )
        """,
        (
            simulation_execution_id,
            evaluation_id,
            digest,
            digest,
            now,
            now,
            retention_expires_at,
        ),
    )
    _require_one_row(cursor, "simulation INSERT")
    cursor.execute(
        f"""
        SELECT simulation_execution_id
        FROM {SIMULATION_TABLE}
        WHERE simulation_execution_id = %s
        """,
        (simulation_execution_id,),
    )
    if cursor.fetchone() != (simulation_execution_id,):
        raise Stage12RuntimeAccessError(
            "Stage 12 runtime could not SELECT its canary simulation"
        )
    cursor.execute(
        f"""
        UPDATE {SIMULATION_TABLE}
        SET updated_at = %s
        WHERE simulation_execution_id = %s
        RETURNING simulation_execution_id
        """,
        (datetime.now(timezone.utc), simulation_execution_id),
    )
    if cursor.fetchone() != (simulation_execution_id,):
        raise Stage12RuntimeAccessError(
            "Stage 12 runtime could not UPDATE its canary simulation"
        )
    cursor.execute(
        f"""
        DELETE FROM {SIMULATION_TABLE}
        WHERE simulation_execution_id = %s
        RETURNING simulation_execution_id
        """,
        (simulation_execution_id,),
    )
    if cursor.fetchone() != (simulation_execution_id,):
        raise Stage12RuntimeAccessError(
            "Stage 12 runtime could not DELETE its canary simulation"
        )
    cursor.execute(
        f"""
        DELETE FROM {REPORT_TABLE}
        WHERE evaluation_id = %s
        RETURNING evaluation_id
        """,
        (evaluation_id,),
    )
    if cursor.fetchone() != (evaluation_id,):
        raise Stage12RuntimeAccessError(
            "Stage 12 runtime could not DELETE its canary report"
        )


def verify_runtime_database(
    database_url: str,
    *,
    expected_role: str,
    environment: str,
    connect: Callable[..., Any] = psycopg.connect,
) -> None:
    """Verify exact runtime identity and required access without retaining rows."""

    if not database_url:
        raise Stage12RuntimeAccessError("Stage 12 database URL is empty")
    if environment not in {"staging", "production"}:
        raise Stage12RuntimeAccessError("environment must be staging or production")
    try:
        with connect(database_url, connect_timeout=10) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT current_user")
                if cursor.fetchone() != (expected_role,):
                    raise Stage12RuntimeAccessError(
                        "Stage 12 credential authenticated as an unexpected role"
                    )
                cursor.execute(
                    """
                    SELECT rolsuper, rolcreaterole, rolcreatedb, rolinherit,
                           rolreplication, rolbypassrls, rolcanlogin
                    FROM pg_roles
                    WHERE rolname = current_user
                    """
                )
                if cursor.fetchone() != EXPECTED_ROLE_ATTRIBUTES:
                    raise Stage12RuntimeAccessError(
                        "Stage 12 runtime role has unsafe role attributes"
                    )
                cursor.execute(
                    """
                    SELECT granted_role.rolname, member_role.rolname
                    FROM pg_auth_members AS membership
                    JOIN pg_roles AS granted_role
                      ON granted_role.oid = membership.roleid
                    JOIN pg_roles AS member_role
                      ON member_role.oid = membership.member
                    WHERE granted_role.rolname = current_user
                       OR member_role.rolname = current_user
                    """
                )
                if cursor.fetchall():
                    raise Stage12RuntimeAccessError(
                        "Stage 12 runtime role participates in role membership"
                    )
                for privilege, expected in (("USAGE", True), ("CREATE", False)):
                    cursor.execute(
                        "SELECT has_schema_privilege(current_user, 'public', %s)",
                        (privilege,),
                    )
                    if cursor.fetchone() != (expected,):
                        raise Stage12RuntimeAccessError(
                            f"Stage 12 runtime has unexpected public-schema {privilege} access"
                        )
                for table in (REPORT_TABLE, SIMULATION_TABLE):
                    for privilege in REQUIRED_TABLE_PRIVILEGES:
                        cursor.execute(
                            "SELECT has_table_privilege(current_user, %s, %s)",
                            (table, privilege),
                        )
                        if cursor.fetchone() != (True,):
                            raise Stage12RuntimeAccessError(
                                f"Stage 12 runtime lacks {privilege} on {table}"
                            )
                    for privilege in FORBIDDEN_TABLE_PRIVILEGES:
                        cursor.execute(
                            "SELECT has_table_privilege(current_user, %s, %s)",
                            (table, privilege),
                        )
                        if cursor.fetchone() != (False,):
                            raise Stage12RuntimeAccessError(
                                f"Stage 12 runtime unexpectedly has {privilege} on {table}"
                            )
                cursor.execute(
                    """
                    SELECT tablename
                    FROM pg_tables
                    WHERE schemaname = 'public'
                      AND ('public.' || tablename) <> ALL(%s)
                      AND (
                        has_table_privilege(current_user, 'public.' || tablename, 'SELECT')
                        OR has_table_privilege(current_user, 'public.' || tablename, 'INSERT')
                        OR has_table_privilege(current_user, 'public.' || tablename, 'UPDATE')
                        OR has_table_privilege(current_user, 'public.' || tablename, 'DELETE')
                      )
                    ORDER BY tablename
                    """,
                    ([REPORT_TABLE, SIMULATION_TABLE],),
                )
                if cursor.fetchall():
                    raise Stage12RuntimeAccessError(
                        "Stage 12 runtime has data access outside its temporary tables"
                    )
                _exercise_runtime_dml(cursor, environment=environment)
            connection.rollback()
    except Stage12RuntimeAccessError:
        raise
    except psycopg.Error as error:
        raise Stage12RuntimeAccessError(
            f"Stage 12 runtime database verification failed ({type(error).__name__})"
        ) from None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-url-file", type=Path, required=True)
    parser.add_argument("--expected-role", required=True)
    parser.add_argument("--environment", required=True)
    return parser


def main() -> None:
    args = _parser().parse_args()
    database_url = args.database_url_file.read_text(encoding="utf-8").strip()
    try:
        verify_runtime_database(
            database_url,
            expected_role=args.expected_role,
            environment=args.environment,
        )
    except Stage12RuntimeAccessError as error:
        raise SystemExit(str(error)) from None
    print(
        "Stage 12 runtime database access verified: "
        f"environment={args.environment}, role={args.expected_role}"
    )


if __name__ == "__main__":
    main()
