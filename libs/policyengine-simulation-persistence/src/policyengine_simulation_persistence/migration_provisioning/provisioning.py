"""Create the simulations and reports of exported v1 report definitions.

Each definition's simulations are created first, keyed by
``legacy_simulation_id``, then its report, keyed by ``legacy_report_id``,
through the stores' ``create_migrated`` methods. A row that already exists is
reused if it matches the definition, so running the same export again creates
nothing. A row that exists with a different definition, or a definition the
database rejects (such as a policy the Public API does not have), fails that
report alone; the other reports continue.

The stores commit each row on its own. A report that fails after some of its
simulations were created leaves those simulations in place, keyed by their v1
IDs, and a later run reuses them.

A dry run only reads: it counts the rows a run would create and the rows that
already exist, and reports the definitions that conflict with existing rows. It
cannot see the Public API's tables, so it does not find a policy or household
the database would reject.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from dataclasses import field as dataclass_field
from typing import TYPE_CHECKING

from sqlalchemy.exc import IntegrityError

if TYPE_CHECKING:
    from collections.abc import Callable
    from uuid import UUID

    from policyengine_simulation_persistence.migration_provisioning import (
        ReportDefinition,
        ReportDefinitionsFile,
        SimulationDefinition,
    )
    from policyengine_simulation_persistence.reports import (
        NewReport,
        ReportRecord,
        ReportStore,
    )
    from policyengine_simulation_persistence.simulations import (
        NewSimulation,
        SimulationRecord,
        SimulationStore,
    )


@dataclass(frozen=True)
class ProvisionedReport:
    """A v1 report and the v2 report that now holds its definition."""

    legacy_report_id: int
    report_id: UUID
    # False when an earlier run had already created the report.
    created: bool


@dataclass(frozen=True)
class ProvisioningFailure:
    """A v1 report whose definition could not be provisioned, and why."""

    legacy_report_id: int
    reason: str


@dataclass
class ProvisioningResult:
    """What one run did, or would do, in the order of the export's definitions.

    In a dry run, ``reports`` lists only the reports that already exist, and
    the ``new_`` counts are the rows a run would create.
    """

    dry_run: bool
    reports: list[ProvisionedReport] = dataclass_field(default_factory=list)
    failures: list[ProvisioningFailure] = dataclass_field(default_factory=list)
    # A simulation several reports use is counted once.
    new_simulations: int = 0
    existing_simulations: int = 0
    new_reports: int = 0
    existing_reports: int = 0


class DefinitionRejectedError(Exception):
    """One report definition cannot be provisioned; the reason says why."""

    def __init__(self, reason: str) -> None:
        """Keep ``reason``, a single line of text."""
        super().__init__(reason)
        self.reason = reason


def _check_matches(
    record: SimulationRecord | ReportRecord,
    expected: NewSimulation | NewReport,
    subject: str,
) -> None:
    differing = [
        field.name
        for field in fields(expected)
        if getattr(record, field.name) != getattr(expected, field.name)
    ]
    if differing:
        msg = f"{subject} is already migrated with a different {', '.join(differing)}"
        raise DefinitionRejectedError(msg)


def _create[RecordT](create: Callable[[], RecordT], subject: str) -> RecordT:
    try:
        return create()
    except IntegrityError as error:
        detail = str(error.orig if error.orig is not None else error)
        first_line = detail.strip().splitlines()[0] if detail.strip() else ""
        msg = f"the database rejected {subject}: {first_line}"
        raise DefinitionRejectedError(msg) from error


class _Provisioner:
    def __init__(
        self,
        simulation_store: SimulationStore,
        report_store: ReportStore,
        *,
        dry_run: bool,
    ) -> None:
        self.simulation_store = simulation_store
        self.report_store = report_store
        self.dry_run = dry_run
        self.result = ProvisioningResult(dry_run=dry_run)
        # v2 simulation IDs by v1 simulation ID, for simulations reports share.
        # In a dry run, None marks a simulation a run would create.
        self.simulation_ids: dict[int, UUID | None] = {}

    def provision(self, definition: ReportDefinition) -> None:
        try:
            simulation_ids = [
                self._simulation_id(definition, simulation)
                for simulation in definition.simulations()
            ]
            self._report(definition, simulation_ids)
        except DefinitionRejectedError as rejection:
            self.result.failures.append(
                ProvisioningFailure(
                    legacy_report_id=definition.legacy_report_id,
                    reason=rejection.reason,
                )
            )

    def _simulation_id(
        self, definition: ReportDefinition, simulation: SimulationDefinition
    ) -> UUID | None:
        legacy_id = simulation.legacy_simulation_id
        if legacy_id in self.simulation_ids:
            return self.simulation_ids[legacy_id]
        subject = f"v1 simulation {legacy_id}"
        new_simulation = definition.new_simulation(simulation)
        record = self.simulation_store.get_by_legacy_simulation_id(legacy_id)
        if record is not None:
            _check_matches(record, new_simulation, subject)
            self.result.existing_simulations += 1
        elif self.dry_run:
            self.result.new_simulations += 1
            self.simulation_ids[legacy_id] = None
            return None
        else:
            record = _create(
                lambda: self.simulation_store.create_migrated(new_simulation), subject
            )
            _check_matches(record, new_simulation, subject)
            self.result.new_simulations += 1
        self.simulation_ids[legacy_id] = record.id
        return record.id

    def _report(
        self, definition: ReportDefinition, simulation_ids: list[UUID | None]
    ) -> None:
        legacy_id = definition.legacy_report_id
        subject = f"v1 report {legacy_id}"
        record = self.report_store.get_by_legacy_report_id(legacy_id)
        existing_ids = [
            simulation_id
            for simulation_id in simulation_ids
            if simulation_id is not None
        ]
        if len(existing_ids) < len(simulation_ids):
            # Only in a dry run: a run would create some of the simulations.
            if record is not None:
                msg = (
                    f"{subject} is already migrated, "
                    "with simulations this run would create"
                )
                raise DefinitionRejectedError(msg)
            self.result.new_reports += 1
            return
        new_report = definition.new_report(
            existing_ids[0], existing_ids[1] if len(existing_ids) > 1 else None
        )
        created = record is None
        if record is None:
            if self.dry_run:
                self.result.new_reports += 1
                return
            record = _create(
                lambda: self.report_store.create_migrated(new_report), subject
            )
        _check_matches(record, new_report, subject)
        if created:
            self.result.new_reports += 1
        else:
            self.result.existing_reports += 1
        self.result.reports.append(
            ProvisionedReport(
                legacy_report_id=legacy_id, report_id=record.id, created=created
            )
        )


def provision_reports(
    definitions: ReportDefinitionsFile,
    simulation_store: SimulationStore,
    report_store: ReportStore,
    *,
    dry_run: bool = False,
) -> ProvisioningResult:
    """Create every definition's simulations and report, and return the result.

    With ``dry_run``, write nothing and return what a run would do. Both stores
    must connect as the runtime role.
    """
    provisioner = _Provisioner(simulation_store, report_store, dry_run=dry_run)
    for definition in definitions.reports:
        provisioner.provision(definition)
    return provisioner.result
