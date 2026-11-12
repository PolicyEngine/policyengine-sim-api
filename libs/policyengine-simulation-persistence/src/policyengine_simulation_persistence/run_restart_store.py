"""The restart check's store: each read and write in a transaction of its own.

The restart check lists the runs and household jobs whose Modal call never
started, marks each one it restarts, and reads back what its call is built
from, each in its own transaction, with Modal calls started between them.
``RunRestartStore`` runs ``run_restart_records``' reads and writes that way.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from policyengine_simulation_persistence.engine import bounded_transaction
from policyengine_simulation_persistence.run_restart_records import (
    list_unstarted_runs,
    mark_restart,
    read_unstarted_report_run,
    read_unstarted_simulation_run,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager
    from datetime import datetime
    from uuid import UUID

    from sqlalchemy import Connection, Engine

    from policyengine_simulation_persistence.run_restart_records import (
        StoredReportRun,
        StoredSimulationRun,
        UnstartedRun,
    )

type TransactionFactory = Callable[[], AbstractContextManager[Connection]]


class RunRestartStore:
    """List, mark and read unstarted runs, each operation in its own transaction."""

    def __init__(self, transaction: TransactionFactory) -> None:
        """Open each transaction with ``transaction``, which commits on success."""
        self._transaction = transaction

    @classmethod
    def from_engine(
        cls, engine: Engine, *, statement_timeout_milliseconds: int
    ) -> RunRestartStore:
        """Use ``engine``, bounding each statement to the given time."""

        def transaction() -> AbstractContextManager[Connection]:
            return bounded_transaction(
                engine, statement_timeout_milliseconds=statement_timeout_milliseconds
            )

        return cls(transaction)

    def list_unstarted(
        self, *, created_after: datetime, created_before: datetime, limit: int
    ) -> tuple[UnstartedRun, ...]:
        """Return the oldest restartable rows created in the window; see the reader."""
        with self._transaction() as connection:
            return list_unstarted_runs(
                connection,
                created_after=created_after,
                created_before=created_before,
                limit=limit,
            )

    def mark_restart(self, run: UnstartedRun) -> bool:
        """Mark the row restarted while it is still unstarted; return whether it was."""
        with self._transaction() as connection:
            return mark_restart(connection, run)

    def read_report_run(self, report_run_id: UUID) -> StoredReportRun | None:
        """Return what the report run's call is built from, or None."""
        with self._transaction() as connection:
            return read_unstarted_report_run(connection, report_run_id)

    def read_simulation_run(
        self, simulation_run_id: UUID
    ) -> StoredSimulationRun | None:
        """Return what the standalone simulation run's call is built from, or None."""
        with self._transaction() as connection:
            return read_unstarted_simulation_run(connection, simulation_run_id)
