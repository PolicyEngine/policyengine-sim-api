from __future__ import annotations

from uuid import UUID

import pytest

from policyengine_simulation_entry.stage12_infrastructure import (
    EXPECTED_ROLE_ATTRIBUTES,
    Stage12RuntimeAccessError,
    verify_runtime_database,
)

EXPECTED_ROLE = "policyengine_stage12_staging"
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
        memberships=(),
        unexpected_tables=(),
        comparison_columns=REQUIRED_COMPARISON_COLUMNS,
    ) -> None:
        self.memberships = list(memberships)
        self.unexpected_tables = list(unexpected_tables)
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
            self.rows = [(EXPECTED_ROLE,)]
        elif "SELECT rolsuper" in normalized:
            self.rows = [EXPECTED_ROLE_ATTRIBUTES]
        elif "FROM pg_auth_members" in normalized:
            self.rows = list(self.memberships)
        elif "has_schema_privilege" in normalized:
            self.rows = [(parameters[0] == "USAGE",)]
        elif "has_table_privilege" in normalized and "FROM pg_tables" not in normalized:
            self.rows = [(parameters[1] in {"SELECT", "INSERT", "UPDATE", "DELETE"},)]
        elif "FROM pg_tables" in normalized:
            self.rows = [(table,) for table in self.unexpected_tables]
        elif "FROM information_schema.columns" in normalized:
            self.rows = list(self.comparison_columns.items())
        elif normalized.startswith("INSERT INTO"):
            self.rows = []
            self.rowcount = 1
        elif normalized.startswith("SELECT evaluation_id"):
            self.rows = [(parameters[0], "pending")]
        elif normalized.startswith("SELECT simulation_execution_id"):
            self.rows = [(parameters[0],)]
        elif normalized.startswith("UPDATE") or normalized.startswith("DELETE"):
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
        call[1]
        and isinstance(call[1][0], list)
        and call[1][0]
        == [
            "public.stage12_evaluation_reports",
            "public.stage12_evaluation_simulations",
        ]
        for call in cursor.calls
    )
    assert any(
        isinstance(parameter, UUID)
        for _, parameters in cursor.calls
        if parameters
        for parameter in parameters
        if not isinstance(parameter, list)
    )


def test_runtime_verification_rejects_any_role_membership() -> None:
    connection = FakeConnection(
        FakeCursor(memberships=[("unexpected_parent", EXPECTED_ROLE)])
    )

    with pytest.raises(Stage12RuntimeAccessError, match="role membership"):
        verify_runtime_database(
            "postgresql://runtime",
            expected_role=EXPECTED_ROLE,
            environment="staging",
            connect=lambda *_, **__: connection,
        )


def test_runtime_verification_rejects_other_table_access() -> None:
    connection = FakeConnection(FakeCursor(unexpected_tables=["reports"]))

    with pytest.raises(Stage12RuntimeAccessError, match="outside its temporary tables"):
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
