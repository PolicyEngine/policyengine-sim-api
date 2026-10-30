"""Find, create and read simulation runs and report runs.

Starting a simulation run reuses the newest attempt for the same simulation,
catalog version and year while it is unfinished, or succeeded within the
reuse window; otherwise, and always when forced, it adds the next attempt.
Concurrent starts settle on one row through the unique attempt number: the
loser of the insert returns the winner's run.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgres_insert

from policyengine_simulation_persistence.reports import ReportKind, reports
from policyengine_simulation_persistence.runs import (
    UNFINISHED_STATUSES,
    NewReportRun,
    NewSimulationRun,
    ReportRunRecord,
    RunStatus,
    SimulationRunRecord,
    StartedSimulationRun,
    report_runs,
    simulation_runs,
)
from policyengine_simulation_persistence.simulations import (
    SimulationKind,
    simulations,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy import ColumnElement, Connection, Engine, RowMapping

# Output files are kept 30 days. A succeeded run is reused only if it finished
# within the last 28, so its files outlive the comparison step that reads them.
SUCCEEDED_RUN_REUSE_WINDOW = timedelta(days=28)


def utc_now() -> datetime:
    """Return the current time in UTC."""
    return datetime.now(UTC)


def _check_region_code(simulation: RowMapping, new_run: NewSimulationRun) -> None:
    """Reject an economy run whose region code is not its simulation's.

    A missing region or dataset, or one on a household run, is left to the
    database's population check.
    """
    if (
        simulation["kind"] == SimulationKind.ECONOMY.value
        and new_run.region_code is not None
        and new_run.region_code != simulation["region_code"]
    ):
        msg = "an economy run's region_code must be its simulation's region code"
        raise ValueError(msg)


def _check_report_run_simulations(
    report: RowMapping,
    baseline: RowMapping | None,
    reform: RowMapping | None,
    new_report_run: NewReportRun,
) -> RowMapping:
    """Require one run of each of the report's simulations, and no other.

    Return the baseline run's key, from which the report run takes its catalog
    version and year.
    """
    expected_reform_run = report["reform_simulation_id"] is not None
    if (
        baseline is None
        or baseline["simulation_id"] != report["baseline_simulation_id"]
        or expected_reform_run != (new_report_run.reform_simulation_run_id is not None)
        or (
            expected_reform_run
            and (
                reform is None
                or reform["simulation_id"] != report["reform_simulation_id"]
            )
        )
    ):
        msg = (
            "a report run needs one existing run of each of the report's "
            "simulations: its baseline and, when it has one, its reform"
        )
        raise ValueError(msg)
    return baseline


class RunStore:
    """Typed SQLAlchemy Core reads and inserts for runs."""

    def __init__(
        self,
        engine: Engine,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        """Use ``engine`` (the runtime role) and ``clock`` for every timestamp."""
        self.engine = engine
        self._clock = clock

    # --- Simulation runs ---------------------------------------------------

    def start_simulation_run(
        self, new_run: NewSimulationRun, *, force: bool = False
    ) -> StartedSimulationRun:
        """Return the reusable run for this setup, or create the next attempt.

        A run is reused while it is unfinished, or when it succeeded within
        ``SUCCEEDED_RUN_REUSE_WINDOW``; a failed run never is. ``force`` always
        adds the next attempt, unless another caller added it at the same
        time, in which case that run is returned. Raise LookupError when the
        simulation does not exist.
        """
        now = self._clock()
        with self.engine.begin() as connection:
            simulation = (
                connection.execute(
                    select(simulations.c.kind, simulations.c.region_code).where(
                        simulations.c.id == new_run.simulation_id
                    )
                )
                .mappings()
                .first()
            )
            if simulation is None:
                msg = f"no simulation {new_run.simulation_id}"
                raise LookupError(msg)
            _check_region_code(simulation, new_run)
            latest = self._latest_simulation_run(
                connection,
                new_run.simulation_id,
                new_run.tax_benefit_model_version_id,
                new_run.year,
            )
            if latest is not None and not force and self._reusable(latest, now):
                return StartedSimulationRun(run=latest, created=False)
            attempt = 1 if latest is None else latest.attempt + 1
            created = (
                connection.execute(
                    postgres_insert(simulation_runs)
                    .values(
                        id=uuid4(),
                        simulation_id=new_run.simulation_id,
                        simulation_kind=simulation["kind"],
                        tax_benefit_model_version_id=(
                            new_run.tax_benefit_model_version_id
                        ),
                        year=new_run.year,
                        attempt=attempt,
                        region_code=new_run.region_code,
                        dataset_id=new_run.dataset_id,
                        trigger=new_run.trigger.value,
                        status=RunStatus.PENDING.value,
                        created_at=now,
                        updated_at=now,
                    )
                    .on_conflict_do_nothing(
                        index_elements=[
                            simulation_runs.c.simulation_id,
                            simulation_runs.c.tax_benefit_model_version_id,
                            simulation_runs.c.year,
                            simulation_runs.c.attempt,
                        ]
                    )
                    .returning(*simulation_runs.c)
                )
                .mappings()
                .first()
            )
            if created is not None:
                return StartedSimulationRun(
                    run=SimulationRunRecord.from_row(created), created=True
                )
            # Another caller added this attempt after the read above; the
            # insert waited for it to commit, so it is visible now.
            concurrent = self._latest_simulation_run(
                connection,
                new_run.simulation_id,
                new_run.tax_benefit_model_version_id,
                new_run.year,
            )
        if concurrent is None:  # pragma: no cover - the conflict proves a row
            msg = "the conflicting simulation run disappeared"
            raise RuntimeError(msg)
        return StartedSimulationRun(run=concurrent, created=False)

    @staticmethod
    def _reusable(run: SimulationRunRecord, now: datetime) -> bool:
        if run.status in UNFINISHED_STATUSES:
            return True
        return (
            run.status is RunStatus.SUCCEEDED
            and run.completed_at is not None
            and run.completed_at >= now - SUCCEEDED_RUN_REUSE_WINDOW
        )

    @staticmethod
    def _latest_simulation_run(
        connection: Connection,
        simulation_id: UUID,
        catalog_version_id: UUID,
        year: int,
    ) -> SimulationRunRecord | None:
        row = (
            connection.execute(
                select(simulation_runs)
                .where(
                    simulation_runs.c.simulation_id == simulation_id,
                    simulation_runs.c.tax_benefit_model_version_id
                    == catalog_version_id,
                    simulation_runs.c.year == year,
                )
                .order_by(simulation_runs.c.attempt.desc())
                .limit(1)
            )
            .mappings()
            .first()
        )
        return None if row is None else SimulationRunRecord.from_row(row)

    def get_simulation_run(self, run_id: UUID) -> SimulationRunRecord | None:
        """Return the simulation run with this ID, or None."""
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    select(simulation_runs).where(simulation_runs.c.id == run_id)
                )
                .mappings()
                .first()
            )
        return None if row is None else SimulationRunRecord.from_row(row)

    def latest_simulation_run(
        self, simulation_id: UUID, catalog_version_id: UUID, year: int
    ) -> SimulationRunRecord | None:
        """Return the newest attempt for this simulation, catalog version and year."""
        with self.engine.connect() as connection:
            return self._latest_simulation_run(
                connection, simulation_id, catalog_version_id, year
            )

    # --- Report runs -------------------------------------------------------

    def create_report_run(self, new_report_run: NewReportRun) -> ReportRunRecord:
        """Add a report run joining two existing simulation runs.

        The report run takes its catalog version and year from its baseline
        run; the database refuses a reform run of another version or year.
        An economy report run starts with a pending comparison step. Raise
        LookupError when the report does not exist, and ValueError when the
        runs are not runs of the report's simulations.
        """
        now = self._clock()
        with self.engine.begin() as connection:
            report = (
                connection.execute(
                    select(
                        reports.c.kind,
                        reports.c.baseline_simulation_id,
                        reports.c.reform_simulation_id,
                    ).where(reports.c.id == new_report_run.report_id)
                )
                .mappings()
                .first()
            )
            if report is None:
                msg = f"no report {new_report_run.report_id}"
                raise LookupError(msg)
            baseline = self._run_key(
                connection, new_report_run.baseline_simulation_run_id
            )
            reform = (
                None
                if new_report_run.reform_simulation_run_id is None
                else self._run_key(connection, new_report_run.reform_simulation_run_id)
            )
            baseline = _check_report_run_simulations(
                report, baseline, reform, new_report_run
            )
            economy = report["kind"] == ReportKind.ECONOMY.value
            row = (
                connection.execute(
                    postgres_insert(report_runs)
                    .values(
                        id=uuid4(),
                        report_id=new_report_run.report_id,
                        report_kind=report["kind"],
                        tax_benefit_model_version_id=(
                            baseline["tax_benefit_model_version_id"]
                        ),
                        year=baseline["year"],
                        baseline_simulation_run_id=(
                            new_report_run.baseline_simulation_run_id
                        ),
                        reform_simulation_run_id=(
                            new_report_run.reform_simulation_run_id
                        ),
                        trigger=new_report_run.trigger.value,
                        comparison_status=(
                            RunStatus.PENDING.value if economy else None
                        ),
                        created_at=now,
                        updated_at=now,
                    )
                    .returning(*report_runs.c)
                )
                .mappings()
                .one()
            )
        return ReportRunRecord.from_row(row)

    @staticmethod
    def _run_key(connection: Connection, run_id: UUID) -> RowMapping | None:
        return (
            connection.execute(
                select(
                    simulation_runs.c.simulation_id,
                    simulation_runs.c.tax_benefit_model_version_id,
                    simulation_runs.c.year,
                ).where(simulation_runs.c.id == run_id)
            )
            .mappings()
            .first()
        )

    def get_report_run(self, report_run_id: UUID) -> ReportRunRecord | None:
        """Return the report run with this ID, or None."""
        return self._one_report_run(report_runs.c.id == report_run_id)

    def latest_report_run(self, report_id: UUID) -> ReportRunRecord | None:
        """Return the report's newest report run, whose status is the report's."""
        return self._one_report_run(report_runs.c.report_id == report_id)

    def _one_report_run(self, condition: ColumnElement[bool]) -> ReportRunRecord | None:
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    select(report_runs)
                    .where(condition)
                    .order_by(report_runs.c.created_at.desc(), report_runs.c.id.desc())
                    .limit(1)
                )
                .mappings()
                .first()
            )
        return None if row is None else ReportRunRecord.from_row(row)
