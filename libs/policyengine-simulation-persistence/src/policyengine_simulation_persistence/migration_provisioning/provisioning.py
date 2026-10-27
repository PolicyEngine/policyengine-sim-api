"""Create the simulations and reports of exported v1 report definitions.

Each definition's simulations are created first, keyed by
``legacy_simulation_id``, then its report, keyed by ``legacy_report_id``,
through the stores' ``create_migrated`` methods. A row that already exists is
reused if it matches the definition, so running the same export again creates
nothing. A row that exists with a different definition, or a definition the
database rejects (such as a policy the Public API does not have), fails that
report alone; the other reports continue. So does a value the database rejects
as data; the file check already rejects those it knows, such as text holding a
NUL character.

Before it creates any of a report's rows, a run reads every row the report
needs that already exists and checks it against the definition, so a conflict
creates nothing. The stores commit each row on its own: a report the database
rejects after some of its simulations were created leaves those simulations in
place, keyed by their v1 IDs, and a later run reuses them.

A dry run only reads: it counts the rows a run would create and the rows that
already exist, and reports the definitions that conflict with existing rows. It
cannot see the Public API's tables, so it does not find a policy or household
the database would reject.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from dataclasses import field as dataclass_field
from decimal import Decimal
from typing import TYPE_CHECKING, overload

from sqlalchemy.exc import DataError, DBAPIError, IntegrityError, SQLAlchemyError

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


def _as_stored(value: object) -> object:
    """Return ``value`` with every JSON number as the exact decimal stored.

    JSONB keeps a number as an exact decimal, so the float 1e23 reads back as
    the integer 100000000000000000000000, which is not equal to the float. A
    float is stored as its shortest decimal form, which ``repr`` gives.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(repr(value))
    if isinstance(value, dict):
        return {key: _as_stored(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_as_stored(item) for item in value]
    return value


@overload
def _check_matches(
    record: SimulationRecord, expected: NewSimulation, subject: str
) -> None: ...


@overload
def _check_matches(record: ReportRecord, expected: NewReport, subject: str) -> None: ...


def _check_matches(
    record: SimulationRecord | ReportRecord,
    expected: NewSimulation | NewReport,
    subject: str,
) -> None:
    differing = [
        field.name
        for field in fields(expected)
        if _as_stored(getattr(record, field.name))
        != _as_stored(getattr(expected, field.name))
    ]
    if differing:
        msg = f"{subject} is already migrated with a different {', '.join(differing)}"
        raise DefinitionRejectedError(msg)


def database_error_summary(error: SQLAlchemyError) -> str:
    """Return the first line of the database's own message for ``error``.

    The driver's message names the constraint or the connection problem, and
    never the password. SQLAlchemy's longer text adds the statement and its
    parameters.
    """
    original = error.orig if isinstance(error, DBAPIError) else None
    lines = str(original if original is not None else error).strip().splitlines()
    return lines[0] if lines else type(error).__name__


def _create[RecordT](create: Callable[[], RecordT], subject: str) -> RecordT:
    try:
        return create()
    except (IntegrityError, DataError) as error:
        # A constraint the row breaks, or a value the database rejects as data
        # that the file check does not know of.
        msg = f"the database rejected {subject}: {database_error_summary(error)}"
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
        # v2 simulation IDs by v1 simulation ID: rows found or created so far.
        self.simulation_ids: dict[int, UUID] = {}
        # v1 simulation IDs a dry run found missing, which a run would create.
        self.simulations_to_create: set[int] = set()

    def provision(self, definition: ReportDefinition) -> None:
        try:
            known_ids = [
                self._existing_simulation_id(definition, simulation)
                for simulation in definition.simulations()
            ]
            existing_report = self._existing_report(definition, known_ids)
            if existing_report is not None:
                self._record(definition, existing_report.id, created=False)
            elif self.dry_run:
                self._count_creations(definition, known_ids)
            else:
                self._create_rows(definition, known_ids)
        except DefinitionRejectedError as rejection:
            self.result.failures.append(
                ProvisioningFailure(
                    legacy_report_id=definition.legacy_report_id,
                    reason=rejection.reason,
                )
            )

    def _existing_simulation_id(
        self, definition: ReportDefinition, simulation: SimulationDefinition
    ) -> UUID | None:
        """Return the existing simulation's v2 ID, or None if a run creates it.

        Reads only, and rejects the report if the row differs from the
        definition.
        """
        legacy_id = simulation.legacy_simulation_id
        if legacy_id in self.simulation_ids:
            return self.simulation_ids[legacy_id]
        if legacy_id in self.simulations_to_create:
            return None
        record = self.simulation_store.get_by_legacy_simulation_id(legacy_id)
        if record is None:
            return None
        _check_matches(
            record, definition.new_simulation(simulation), f"v1 simulation {legacy_id}"
        )
        self.simulation_ids[legacy_id] = record.id
        self.result.existing_simulations += 1
        return record.id

    def _existing_report(
        self, definition: ReportDefinition, known_ids: list[UUID | None]
    ) -> ReportRecord | None:
        """Return the existing report, if it matches the definition. Reads only."""
        legacy_id = definition.legacy_report_id
        subject = f"v1 report {legacy_id}"
        record = self.report_store.get_by_legacy_report_id(legacy_id)
        if record is None:
            return None
        simulation_ids = [
            simulation_id for simulation_id in known_ids if simulation_id is not None
        ]
        if len(simulation_ids) < len(known_ids):
            msg = (
                f"{subject} is already migrated, with simulations this run would create"
            )
            raise DefinitionRejectedError(msg)
        _check_matches(record, _new_report(definition, simulation_ids), subject)
        return record

    def _count_creations(
        self, definition: ReportDefinition, known_ids: list[UUID | None]
    ) -> None:
        for simulation, known_id in zip(
            definition.simulations(), known_ids, strict=True
        ):
            legacy_id = simulation.legacy_simulation_id
            if known_id is None and legacy_id not in self.simulations_to_create:
                self.simulations_to_create.add(legacy_id)
                self.result.new_simulations += 1
        self.result.new_reports += 1

    def _create_rows(
        self, definition: ReportDefinition, known_ids: list[UUID | None]
    ) -> None:
        simulation_ids = [
            known_id
            if known_id is not None
            else self._created_simulation_id(definition, simulation)
            for simulation, known_id in zip(
                definition.simulations(), known_ids, strict=True
            )
        ]
        subject = f"v1 report {definition.legacy_report_id}"
        new_report = _new_report(definition, simulation_ids)
        record = _create(lambda: self.report_store.create_migrated(new_report), subject)
        # Another writer may have created the report since it was read.
        _check_matches(record, new_report, subject)
        self.result.new_reports += 1
        self._record(definition, record.id, created=True)

    def _created_simulation_id(
        self, definition: ReportDefinition, simulation: SimulationDefinition
    ) -> UUID:
        legacy_id = simulation.legacy_simulation_id
        if legacy_id in self.simulation_ids:
            # The same v1 simulation is this report's baseline and reform.
            return self.simulation_ids[legacy_id]
        subject = f"v1 simulation {legacy_id}"
        new_simulation = definition.new_simulation(simulation)
        record = _create(
            lambda: self.simulation_store.create_migrated(new_simulation), subject
        )
        _check_matches(record, new_simulation, subject)
        self.simulation_ids[legacy_id] = record.id
        self.result.new_simulations += 1
        return record.id

    def _record(
        self, definition: ReportDefinition, report_id: UUID, *, created: bool
    ) -> None:
        if not created:
            self.result.existing_reports += 1
        self.result.reports.append(
            ProvisionedReport(
                legacy_report_id=definition.legacy_report_id,
                report_id=report_id,
                created=created,
            )
        )


def _new_report(definition: ReportDefinition, simulation_ids: list[UUID]) -> NewReport:
    return definition.new_report(
        simulation_ids[0], simulation_ids[1] if len(simulation_ids) > 1 else None
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
