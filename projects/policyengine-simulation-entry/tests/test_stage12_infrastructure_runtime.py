from __future__ import annotations

from uuid import UUID

import pytest

from policyengine_simulation_entry.stage12_infrastructure import (
    Stage12RuntimeAccessError,
    verify_runtime_database,
)

EXPECTED_ROLE = "policyengine_v2_runtime"
REQUIRED_COMPARISON_COLUMNS = {
    "comparison_status",
    "comparison_output_uri",
    "comparison_output_sha256",
    "comparison_schema_version",
    "comparison_completed_at",
    "comparison_error_code",
    "comparison_error_summary",
}


class FakeCursor:
    def __init__(
        self,
        *,
        current_role=EXPECTED_ROLE,
        comparison_columns=REQUIRED_COMPARISON_COLUMNS,
    ) -> None:
        self.current_role = current_role
        self.comparison_columns = {
            column: (
                "v2_stage12_result_comparison_status"
                if column == "comparison_status"
                else "text"
            )
            for column in comparison_columns
        }
        self.rows = []
        self.rowcount = -1
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def execute(self, statement, parameters=None) -> None:
        normalized = " ".join(statement.split())
        self.calls.append((normalized, parameters))
        self.rowcount = -1
        if normalized == "SELECT current_user":
            self.rows = [(self.current_role,)]
        elif "has_schema_privilege" in normalized:
            self.rows = [(parameters[0] == "USAGE",)]
        elif "has_table_privilege" in normalized and "FROM pg_tables" not in normalized:
            self.rows = [(parameters[1] in {"SELECT", "INSERT", "UPDATE", "DELETE"},)]
        elif "FROM information_schema.columns" in normalized:
            self.rows = list(self.comparison_columns.items())
        elif normalized.startswith("INSERT INTO"):
            self.rows = []
            self.rowcount = 1
        elif normalized.startswith("SELECT evaluation_id"):
            self.rows = [(parameters[0], "pending")]
        elif normalized.startswith("SELECT simulation_execution_id"):
            self.rows = [(parameters[0],)]
        elif normalized.startswith(("UPDATE", "DELETE")):
            identifier = (
                parameters[-1] if normalized.startswith("UPDATE") else parameters[0]
            )
            self.rows = [
                (identifier, "running")
                if "comparison_status = 'running'" in normalized
                else (identifier,)
            ]
            self.rowcount = 1
        else:  # pragma: no cover - protects the fake from silent query drift
            raise AssertionError(f"unexpected query: {normalized}")

    def fetchone(self):
        return self.rows.pop(0)

    def fetchall(self):
        rows, self.rows = self.rows, []
        return rows


class FakeConnection:
    def __init__(self, cursor: FakeCursor) -> None:
        self._cursor = cursor
        self.rollback_called = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def cursor(self) -> FakeCursor:
        return self._cursor

    def rollback(self) -> None:
        self.rollback_called = True


def test_runtime_verification_exercises_required_dml_and_rolls_back() -> None:
    cursor = FakeCursor()
    connection = FakeConnection(cursor)

    verify_runtime_database(
        "postgresql://runtime",
        expected_role=EXPECTED_ROLE,
        environment="staging",
        connect=lambda *_, **__: connection,
    )

    assert connection.rollback_called is True
    operations = [call[0].split(maxsplit=1)[0] for call in cursor.calls]
    assert operations.count("INSERT") == 2
    assert operations.count("UPDATE") == 2
    assert operations.count("DELETE") == 2
    assert any(
        "FROM information_schema.columns" in statement for statement, _ in cursor.calls
    )
    assert any(
        statement.startswith("INSERT INTO") and "comparison_status" in statement
        for statement, _ in cursor.calls
    )
    assert any(
        isinstance(parameter, UUID)
        for _, parameters in cursor.calls
        if parameters
        for parameter in parameters
        if not isinstance(parameter, list)
    )
    assert not any("SELECT rolsuper" in statement for statement, _ in cursor.calls)
    assert not any("FROM pg_auth_members" in statement for statement, _ in cursor.calls)
    assert not any("FROM pg_tables" in statement for statement, _ in cursor.calls)


def test_runtime_verification_rejects_sqlalchemy_psycopg_url() -> None:
    with pytest.raises(Stage12RuntimeAccessError, match="must use postgresql://"):
        verify_runtime_database(
            "postgresql+psycopg://runtime",
            expected_role=EXPECTED_ROLE,
            environment="staging",
            connect=lambda *_args, **_kwargs: pytest.fail(
                "invalid URL must be rejected before connecting"
            ),
        )


def test_runtime_verification_requires_shared_v2_runtime_role() -> None:
    connection = FakeConnection(FakeCursor(current_role="policyengine_stage12_staging"))

    with pytest.raises(Stage12RuntimeAccessError, match="unexpected role"):
        verify_runtime_database(
            "postgresql://runtime",
            expected_role=EXPECTED_ROLE,
            environment="staging",
            connect=lambda *_, **__: connection,
        )


def test_runtime_verification_rejects_missing_comparison_migration() -> None:
    connection = FakeConnection(
        FakeCursor(
            comparison_columns=REQUIRED_COMPARISON_COLUMNS
            - {"comparison_error_summary"}
        )
    )

    with pytest.raises(
        Stage12RuntimeAccessError,
        match="comparison Alembic migration.*comparison_error_summary",
    ):
        verify_runtime_database(
            "postgresql://runtime",
            expected_role=EXPECTED_ROLE,
            environment="staging",
            connect=lambda *_, **__: connection,
        )
