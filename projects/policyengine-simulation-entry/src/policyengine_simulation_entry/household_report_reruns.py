"""Re-run a stored household report on the current .py version.

A household report's re-run calculates its stored household again, in the
report's own year, under each side's stored policy, on the current
versions, and keeps every earlier run. The re-run service
(``economy_reruns``) reads the stored report and hands a household report
to ``HouseholdReportRerunService``, which:

1. finds the v2 version manifest's default worker's household function for
   the country;
2. reads, in one transaction, the country's catalog version for that
   worker's .py version, its model, the stored household and each side's
   policy values, and checks each side's household contract request;
3. for a keyed report's unforced re-run, joins, in one transaction, the
   report's run on the current versions that hasn't failed and wasn't
   forced, or else creates the report's new run (trigger ``manual``)
   naming each side's household simulation run on those versions, found or
   inserted, as an economy report's re-run does (C1 · 24's answer 8). A
   forced re-run, and every re-run of a report created before keys, whose
   runs may have been calculated for it alone (interim answer B), creates
   an extra report run and a new run of each side, all marked forced,
   which nothing joins and nothing refuses. Every new run is pending with
   no Modal call ID;
4. for a report run it created, starts one household function call and
   records its ID, as for a new report, with each side's simulation run as
   its calculation ID, failing the new report run when Modal refuses the
   call (and a forced re-run's simulation runs, which only it names). A
   joined run gets no call.

The re-run service hands over only a report naming each row this needs and
no dynamic, which a household calculation does not apply. Whatever is
missing or cannot be calculated is refused in steps 1 and 2, before
anything is written, located at the stored report.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Protocol

from policyengine_simulation_persistence import (
    HouseholdReportStore,
    HouseholdRerunSide,
    NewHouseholdReportRerun,
    RunVersions,
)

from policyengine_simulation_entry.economy_submission_errors import (
    REQUEST_BODY,
    STORED_REPORT,
    EconomySubmissionUnprocessableError,
    rerun_refusals,
)
from policyengine_simulation_entry.household_report_calculations import (
    side_runs_calculation,
)
from policyengine_simulation_entry.household_report_submissions import (
    HouseholdReports,
    HouseholdReportSubmissionService,
    household_report_created,
)
from policyengine_simulation_entry.household_workers import (
    ManifestHouseholdWorkers,
    ModalHouseholdFunctionCaller,
)
from policyengine_simulation_entry.simulation_database import SimulationDatabase

if TYPE_CHECKING:
    from collections.abc import Mapping
    from uuid import UUID

    from policyengine_simulation_contract.household_report_execution import (
        HouseholdReportRunCalculation,
    )
    from policyengine_simulation_contract.stage12_bundle import CountryId
    from policyengine_simulation_persistence import (
        FoundReportRun,
        HouseholdReportInputs,
        ReportSide,
        SideDefinition,
    )

    from policyengine_simulation_entry.config import Settings
    from policyengine_simulation_entry.economy_submission_errors import (
        UnprocessableReason,
    )
    from policyengine_simulation_entry.entry_runtime import (
        SimulationDatabaseConnection,
    )
    from policyengine_simulation_entry.household_report_api_models import (
        HouseholdReportCreated,
    )
    from policyengine_simulation_entry.household_workers import HouseholdWorker

# How long one of the service's statements may run, as a household job's.
STATEMENT_TIMEOUT_MILLISECONDS = 5_000


@dataclass(frozen=True)
class StoredHouseholdReport:
    """A stored household report a re-run calculates again, each row it needs present.

    ``baseline`` and ``reform`` are the report's own household simulations,
    with their stored policies; ``reform`` is None for a report of the
    baseline alone. Neither names a dynamic, which a household calculation
    does not apply: the re-run service refuses a report whose side does.
    ``keyed`` says the report was found or created by key (C1 · 27).
    """

    report_id: UUID
    country: CountryId
    household_id: UUID
    year: int
    baseline: SideDefinition
    reform: SideDefinition | None
    keyed: bool = False

    @property
    def sides(self) -> dict[ReportSide, SideDefinition]:
        """Return each side the report calculates, the baseline's first."""
        sides: dict[ReportSide, SideDefinition] = {"baseline": self.baseline}
        if self.reform is not None:
            sides["reform"] = self.reform
        return sides


class HouseholdReportRerunRows(HouseholdReports, Protocol):
    """The household report reads and writes, with a stored report's re-run.

    ``HouseholdReportStore`` is one.
    """

    def create_report_rerun(self, rerun: NewHouseholdReportRerun) -> FoundReportRun:
        """Join the report's run on the re-run's versions, or create it."""
        ...


class HouseholdReportReruns(Protocol):
    """Re-run stored household reports (``HouseholdReportRerunService``)."""

    async def rerun_report(
        self,
        stored: StoredHouseholdReport,
        *,
        force: bool,
        observability_context: dict[str, Any] | None,
    ) -> HouseholdReportCreated:
        """Join or create the stored report's run, and start a run it created."""
        ...


class HouseholdReportRerunService(
    HouseholdReportSubmissionService[HouseholdReportRerunRows]
):
    """Create and re-run household reports; each run is one household function call."""

    async def rerun_report(
        self,
        stored: StoredHouseholdReport,
        *,
        force: bool,
        observability_context: dict[str, Any] | None,
    ) -> HouseholdReportCreated:
        """Join or create the stored report's run on the current versions.

        ``force``, or a report without keys, makes it an extra forced run.
        A run it created is started; one it joined is answered as it is.
        Raise EconomyRecordNotFoundError (404) for a report deleted after it
        was read, and otherwise the errors ``submit_report`` raises, located
        at the stored report; a 503 for Modal's refusal names the new run as
        well as the report.
        """
        worker = await self._default_worker(stored.country)
        inputs = await self._read_inputs(
            country=stored.country,
            household_id=stored.household_id,
            policy_ids={
                side: definition.choice.policy_id
                for side, definition in stored.sides.items()
            },
            worker=worker,
            source=STORED_REPORT,
        )
        run_id = self._new_id()
        new_sides = {
            side: HouseholdRerunSide(
                simulation_id=definition.simulation_id,
                simulation_run_id=self._new_id(),
            )
            for side, definition in stored.sides.items()
        }
        forced = force or not stored.keyed
        rerun = NewHouseholdReportRerun(
            report_id=stored.report_id,
            run_id=run_id,
            tax_benefit_model_version_id=inputs.catalog_version_id,
            year=stored.year,
            versions=RunVersions(
                country_package_version=worker.bundle.country_package_version,
                policyengine_version=worker.bundle.policyengine_version,
            ),
            baseline=new_sides["baseline"],
            reform=new_sides.get("reform"),
            forced=forced,
        )
        # Refused before anything is written, on the run IDs it proposes.
        _stored_report_calculation(
            inputs,
            year=rerun.year,
            run_ids={
                side: proposed.simulation_run_id
                for side, proposed in rerun.named_sides.items()
            },
            worker=worker,
        )
        with rerun_refusals("report"):
            found = await self._create(
                lambda: self._store.create_report_rerun(rerun),
                rerun=True,
                source=STORED_REPORT,
            )
        run_ids = _side_run_ids(found, rerun.named_sides)
        status = found.status
        if found.inserted:
            status = await self._start_report_run(
                worker,
                _stored_report_calculation(
                    inputs, year=rerun.year, run_ids=run_ids, worker=worker
                ),
                report_id=rerun.report_id,
                run_id=found.id,
                # Only a forced run's simulation runs are this re-run's alone.
                simulation_run_ids=rerun.simulation_run_ids if forced else (),
                observability_context=observability_context,
                rerun=True,
            )
        sides = {
            side: HouseholdRerunSide(
                simulation_id=definition.simulation_id, simulation_run_id=run_ids[side]
            )
            for side, definition in stored.sides.items()
        }
        return household_report_created(
            report_id=rerun.report_id,
            run_id=found.id,
            status=status,
            year=rerun.year,
            baseline=sides["baseline"],
            reform=sides.get("reform"),
        )


def _side_run_ids(
    run: FoundReportRun, sides: Mapping[ReportSide, object]
) -> dict[ReportSide, UUID]:
    """Return the simulation run the report run names for each of ``sides``."""
    named: dict[ReportSide, UUID | None] = {
        "baseline": run.baseline_simulation_run_id,
        "reform": run.reform_simulation_run_id,
    }
    run_ids: dict[ReportSide, UUID] = {}
    for side in sides:
        run_id = named[side]
        if run_id is None:
            msg = f"report run {run.id} of a household report names no {side} run"
            raise ValueError(msg)
        run_ids[side] = run_id
    return run_ids


def _stored_report_calculation(
    inputs: HouseholdReportInputs,
    *,
    year: int,
    run_ids: Mapping[ReportSide, UUID],
    worker: HouseholdWorker,
) -> HouseholdReportRunCalculation:
    """Build the re-run's household function payload, in the stored report's year.

    A household or policy the contract refuses is located at the stored
    report's fields rather than a request's.
    """
    try:
        return side_runs_calculation(inputs, year=year, run_ids=run_ids, worker=worker)
    except EconomySubmissionUnprocessableError as error:
        raise EconomySubmissionUnprocessableError(
            code=error.code,
            reasons=tuple(_at_stored_report(reason) for reason in error.reasons),
        ) from None


def _at_stored_report(reason: UnprocessableReason) -> UnprocessableReason:
    """Locate a reason at the stored report's field rather than a request's."""
    if reason.loc[:1] != (REQUEST_BODY.location,):
        return reason
    return replace(reason, loc=(STORED_REPORT.location, *reason.loc[1:]))


def household_reports_for(
    settings: Settings, database: SimulationDatabaseConnection | None
) -> HouseholdReportRerunService | None:
    """Build the household report service as the household calculation one is.

    It creates and re-runs household reports. It needs the simulation
    database and the v2 Modal environment; without them household reports
    answer 503. Nothing is read from Modal until the first submission.
    """
    if not settings.household_calculations_configured or not isinstance(
        database, SimulationDatabase
    ):
        return None
    return HouseholdReportRerunService(
        store=HouseholdReportStore.from_engine(
            database.engine,
            statement_timeout_milliseconds=STATEMENT_TIMEOUT_MILLISECONDS,
        ),
        workers=ManifestHouseholdWorkers.from_modal(
            manifest_name=settings.v2_manifest_name,
            environment=settings.v2_modal_environment,
        ),
        caller=ModalHouseholdFunctionCaller(settings.v2_modal_environment),
    )
