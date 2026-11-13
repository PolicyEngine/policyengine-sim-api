"""Re-run a stored household report on the current .py version.

A household report's re-run calculates its stored household again, in the
report's own year, under each side's stored policy, as a new run, and keeps
every earlier run. The re-run service (``economy_reruns``) reads the stored
report and hands a household report to ``HouseholdReportRerunService``,
which:

1. finds the v2 version manifest's default worker's household function for
   the country;
2. reads, in one transaction, the country's catalog version for that
   worker's .py version, its model, the stored household and each side's
   policy values, and builds each side's household contract request with
   the side's new simulation run ID as its calculation ID;
3. creates, in one transaction, the report's new run (trigger ``manual``)
   and a new run of each side's own household simulation, all pending with
   no Modal call ID, unless the report has a run pending or running;
4. starts one household function call for the new report run and records
   its ID, as for a new report, failing the new runs when Modal refuses the
   call.

A household run is never reused, so a forced re-run creates the same runs.
Whatever is missing or cannot be calculated is refused in steps 1 and 2,
before anything is written, located at the stored report.
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
from policyengine_simulation_entry.household_report_api_models import (
    HouseholdReportCreated,
)
from policyengine_simulation_entry.household_report_submissions import (
    HouseholdReports,
    HouseholdReportSideInputs,
    HouseholdReportSubmissionService,
    household_report_calculation,
)
from policyengine_simulation_entry.household_workers import (
    ManifestHouseholdWorkers,
    ModalHouseholdFunctionCaller,
)
from policyengine_simulation_entry.simulation_database import SimulationDatabase

if TYPE_CHECKING:
    from uuid import UUID

    from policyengine_simulation_contract.household_report_execution import (
        HouseholdReportRunCalculation,
    )
    from policyengine_simulation_contract.stage12_bundle import CountryId
    from policyengine_simulation_persistence import (
        PolicyReform,
        ReportSide,
        SideDefinition,
        StoredHousehold,
    )

    from policyengine_simulation_entry.config import Settings
    from policyengine_simulation_entry.economy_submission_errors import (
        UnprocessableReason,
    )
    from policyengine_simulation_entry.entry_runtime import (
        SimulationDatabaseConnection,
    )
    from policyengine_simulation_entry.household_workers import HouseholdWorker

# How long one of the service's statements may run, as a household job's.
STATEMENT_TIMEOUT_MILLISECONDS = 5_000


@dataclass(frozen=True)
class StoredHouseholdReport:
    """A stored household report a re-run calculates again, each row it needs present.

    ``baseline`` and ``reform`` are the report's own household simulations,
    with their stored policies; ``reform`` is None for a report of the
    baseline alone.
    """

    report_id: UUID
    country: CountryId
    household_id: UUID
    year: int
    baseline: SideDefinition
    reform: SideDefinition | None

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

    def create_report_rerun(self, rerun: NewHouseholdReportRerun) -> None:
        """Create the report's new run and each side's new simulation run."""
        ...


class HouseholdReportRerunService(
    HouseholdReportSubmissionService[HouseholdReportRerunRows]
):
    """Create and re-run household reports; each run is one household function call."""

    async def rerun_report(
        self,
        stored: StoredHouseholdReport,
        *,
        observability_context: dict[str, Any] | None,
    ) -> HouseholdReportCreated:
        """Create the stored report's new run on the current versions, and start it.

        Raise EconomyRecordNotFoundError (404) for a report deleted after it
        was read, EconomyRerunConflictError (409 ``run_in_progress``) while
        it has a run pending or running, and otherwise the errors
        ``submit_report`` raises, located at the stored report; a 503 for
        Modal's refusal names the new run as well as the report.
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
        )
        calculation = _stored_report_calculation(
            inputs.household, rerun, reforms=inputs.reforms, worker=worker
        )
        with rerun_refusals("report"):
            await self._create(
                lambda: self._store.create_report_rerun(rerun),
                rerun=True,
                source=STORED_REPORT,
            )
        status = await self._start_report_run(
            worker,
            calculation,
            report_id=rerun.report_id,
            run_id=rerun.run_id,
            simulation_run_ids=rerun.simulation_run_ids,
            observability_context=observability_context,
            rerun=True,
        )
        return HouseholdReportCreated(
            report_id=rerun.report_id,
            report_run_id=rerun.run_id,
            status=status,
            year=rerun.year,
            baseline_simulation_id=rerun.baseline.simulation_id,
            baseline_simulation_run_id=rerun.baseline.simulation_run_id,
            reform_simulation_id=(
                None if rerun.reform is None else rerun.reform.simulation_id
            ),
            reform_simulation_run_id=(
                None if rerun.reform is None else rerun.reform.simulation_run_id
            ),
        )


def _stored_report_calculation(
    household: StoredHousehold,
    rerun: NewHouseholdReportRerun,
    *,
    reforms: dict[ReportSide, PolicyReform],
    worker: HouseholdWorker,
) -> HouseholdReportRunCalculation:
    """Build the re-run's household function payload, in the stored report's year.

    A household or policy the contract refuses is located at the stored
    report's fields rather than a request's.
    """

    def side_inputs(
        side: HouseholdRerunSide, name: ReportSide
    ) -> HouseholdReportSideInputs:
        return HouseholdReportSideInputs(
            simulation_run_id=side.simulation_run_id, policy_values=reforms[name]
        )

    try:
        return household_report_calculation(
            household=household,
            year=rerun.year,
            baseline=side_inputs(rerun.baseline, "baseline"),
            reform=None
            if rerun.reform is None
            else side_inputs(rerun.reform, "reform"),
            worker=worker,
        )
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
