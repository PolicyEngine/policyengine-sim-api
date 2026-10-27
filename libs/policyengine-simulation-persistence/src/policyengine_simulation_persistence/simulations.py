"""``simulation.simulations``: what to run, independent of model versions.

A simulation is one policy applied to one population: a household, or an
economy named by a region code (``state/ca``) and an optional dataset name. It
is a definition, not an execution, and never changes once created. A null
policy means current law, and a null dataset name means the region's default
dataset in whichever catalog version a run uses, so the same simulation can
run again on a newer catalog version.

Foreign keys into the Public API's tables (``public_api``) tie the policy and
the household to the simulation's country, and name an optional dynamic. The
region and dataset are not foreign keys here, because their rows belong to one
catalog version; each run resolves them in its own.

A simulation migrated from the Public API's v1 database keeps its v1
simulation ID in ``legacy_simulation_id``. That database is separate, so the v1
ID is a plain unique column, not a foreign key.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, fields
from enum import StrEnum
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Index,
    String,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import insert as postgres_insert

from policyengine_simulation_persistence import public_api
from policyengine_simulation_persistence.schema import simulation_metadata

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy import ColumnElement, Connection, Engine, RowMapping

COUNTRY_PATTERN = re.compile(r"[a-z]{2}")
# Region codes and dataset names are the Public API's catalog keys: up to 255
# characters, with no whitespace.
CATALOG_KEY_PATTERN = re.compile(r"\S{1,255}")
CATALOG_KEY_LENGTH = 255


class SimulationKind(StrEnum):
    """What a simulation runs on: one household or a whole economy."""

    ECONOMY = "economy"
    HOUSEHOLD = "household"


_KIND_VALUES = ", ".join(f"'{kind.value}'" for kind in SimulationKind)

simulations = Table(
    "simulations",
    simulation_metadata,
    Column("id", Uuid(as_uuid=True), primary_key=True),
    Column("kind", Text, nullable=False),
    Column("country", Text, nullable=False),
    Column("policy_id", Uuid(as_uuid=True), nullable=True),
    Column("dynamic_id", Uuid(as_uuid=True), nullable=True),
    Column("household_id", Uuid(as_uuid=True), nullable=True),
    Column("region_code", String(CATALOG_KEY_LENGTH), nullable=True),
    Column("dataset_name", String(CATALOG_KEY_LENGTH), nullable=True),
    Column("legacy_simulation_id", BigInteger, nullable=True, unique=True),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    CheckConstraint(f"kind IN ({_KIND_VALUES})", name="kind"),
    CheckConstraint("country ~ '^[a-z]{2}$'", name="country"),
    CheckConstraint(
        "(kind = 'household' AND household_id IS NOT NULL "
        "AND region_code IS NULL AND dataset_name IS NULL) "
        "OR (kind = 'economy' AND household_id IS NULL "
        "AND region_code IS NOT NULL)",
        name="population_matches_kind",
    ),
    CheckConstraint(
        "region_code IS NULL OR region_code ~ '^\\S+$'", name="region_code"
    ),
    CheckConstraint(
        "dataset_name IS NULL OR dataset_name ~ '^\\S+$'", name="dataset_name"
    ),
    CheckConstraint(
        "legacy_simulation_id IS NULL OR legacy_simulation_id > 0",
        name="legacy_simulation_id",
    ),
    # Reports refer to a simulation together with its country and kind, so a
    # report can only compare simulations of its own country and kind.
    UniqueConstraint("id", "country", "kind"),
    # Simulation runs refer to a simulation together with its kind, so a run
    # names a region and dataset exactly when its simulation is an economy.
    UniqueConstraint("id", "kind"),
    ForeignKeyConstraint(
        ["policy_id", "country"],
        [public_api.policies.c.id, public_api.policies.c.country_id],
        name="fk_simulations_policy_country",
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["dynamic_id"],
        [public_api.dynamics.c.id],
        name="fk_simulations_dynamic",
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["household_id", "country"],
        [public_api.households.c.id, public_api.households.c.country_id],
        name="fk_simulations_household_country",
        ondelete="RESTRICT",
    ),
)
# A Public API delete of a policy, household or dynamic checks these for
# references.
Index("ix_simulations_policy_id", simulations.c.policy_id)
Index("ix_simulations_household_id", simulations.c.household_id)
Index("ix_simulations_dynamic_id", simulations.c.dynamic_id)


def _check_catalog_key(field_name: str, value: str | None) -> None:
    if value is not None and not CATALOG_KEY_PATTERN.fullmatch(value):
        msg = f"{field_name} must be 1 to 255 characters with no whitespace"
        raise ValueError(msg)


def check_legacy_id(field_name: str, value: int | None) -> None:
    """Reject a v1 ID that is not a positive integer."""
    if value is not None and value <= 0:
        msg = f"{field_name} must be a positive v1 ID"
        raise ValueError(msg)


def parse_kind[KindT: StrEnum](value: object, kind_type: type[KindT]) -> KindT:
    """Return the ``kind_type`` member for ``value``, which may be its text.

    Raise ValueError naming ``kind`` when the value is not one of its members.
    """
    try:
        return kind_type(value)
    except ValueError:
        msg = f"kind must be one of {', '.join(kind_type)}"
        raise ValueError(msg) from None


def check_country(country: str) -> None:
    """Reject a country that is not a lowercase two-letter code."""
    if not COUNTRY_PATTERN.fullmatch(country):
        msg = "country must be a lowercase two-letter code"
        raise ValueError(msg)


def insert_or_get_existing(
    connection: Connection,
    table: Table,
    values: dict[str, Any],
    unique_column: Column[Any],
) -> RowMapping:
    """Insert a row, or return the row that already has its unique value."""
    created = (
        connection.execute(
            postgres_insert(table)
            .values(**values)
            .on_conflict_do_nothing(index_elements=[unique_column])
            .returning(*table.c)
        )
        .mappings()
        .first()
    )
    if created is not None:
        return created
    return (
        connection.execute(
            select(table).where(unique_column == values[unique_column.name])
        )
        .mappings()
        .one()
    )


@dataclass(frozen=True)
class NewSimulation:
    """A simulation to create.

    The database checks that the population matches the kind and that the
    policy, household and dynamic exist for the country; the fields are
    checked here first.
    """

    kind: SimulationKind
    country: str
    policy_id: UUID | None = None
    dynamic_id: UUID | None = None
    household_id: UUID | None = None
    region_code: str | None = None
    dataset_name: str | None = None
    legacy_simulation_id: int | None = None

    def __post_init__(self) -> None:
        """Reject malformed fields without touching the database."""
        # A frozen dataclass sets its own field through object.__setattr__.
        object.__setattr__(self, "kind", parse_kind(self.kind, SimulationKind))
        check_country(self.country)
        _check_catalog_key("region_code", self.region_code)
        _check_catalog_key("dataset_name", self.dataset_name)
        check_legacy_id("legacy_simulation_id", self.legacy_simulation_id)


@dataclass(frozen=True)
class SimulationRecord:
    """One ``simulation.simulations`` row."""

    id: UUID
    kind: SimulationKind
    country: str
    policy_id: UUID | None
    dynamic_id: UUID | None
    household_id: UUID | None
    region_code: str | None
    dataset_name: str | None
    legacy_simulation_id: int | None
    created_at: datetime

    @classmethod
    def from_row(cls, row: RowMapping) -> SimulationRecord:
        """Build the record from a selected ``simulations`` row."""
        values = {field.name: row[field.name] for field in fields(cls)}
        values["kind"] = SimulationKind(values["kind"])
        return cls(**values)


def _new_simulation_values(new_simulation: NewSimulation) -> dict[str, Any]:
    return {
        "id": uuid4(),
        **{
            field.name: getattr(new_simulation, field.name)
            for field in fields(new_simulation)
        },
        "kind": new_simulation.kind.value,
    }


class SimulationStore:
    """Typed SQLAlchemy Core reads and inserts for simulation rows."""

    def __init__(self, engine: Engine) -> None:
        """Use ``engine``, which connects as the runtime role."""
        self.engine = engine

    def create(self, new_simulation: NewSimulation) -> SimulationRecord:
        """Insert one simulation and return it."""
        with self.engine.begin() as connection:
            row = (
                connection.execute(
                    postgres_insert(simulations)
                    .values(**_new_simulation_values(new_simulation))
                    .returning(*simulations.c)
                )
                .mappings()
                .one()
            )
        return SimulationRecord.from_row(row)

    def find_or_create(self, new_simulation: NewSimulation) -> SimulationRecord:
        """Return the oldest simulation with this definition, or create it.

        Runs are reused per simulation, so a submission that describes a
        simulation already defined, such as current law in California, uses
        that simulation and can reuse its runs. Two concurrent submissions of
        a new definition can each create one; both work, and later
        submissions use the older.
        """
        values = _new_simulation_values(new_simulation)
        definition = [
            simulations.c[name].is_not_distinct_from(values[name])
            for name in (
                "kind",
                "country",
                "policy_id",
                "dynamic_id",
                "household_id",
                "region_code",
                "dataset_name",
            )
        ]
        with self.engine.begin() as connection:
            row = (
                connection.execute(
                    select(simulations)
                    .where(*definition)
                    .order_by(simulations.c.created_at, simulations.c.id)
                    .limit(1)
                )
                .mappings()
                .first()
            )
            if row is None:
                row = (
                    connection.execute(
                        postgres_insert(simulations)
                        .values(**values)
                        .returning(*simulations.c)
                    )
                    .mappings()
                    .one()
                )
        return SimulationRecord.from_row(row)

    def create_migrated(self, new_simulation: NewSimulation) -> SimulationRecord:
        """Create a simulation for a v1 simulation, or return the existing one.

        The v1 simulation ID must be set. An existing row is returned as it is,
        even if ``new_simulation`` describes it differently, so running the
        one-time migration again creates nothing new.
        """
        if new_simulation.legacy_simulation_id is None:
            msg = "a migrated simulation needs its legacy_simulation_id"
            raise ValueError(msg)
        with self.engine.begin() as connection:
            row = insert_or_get_existing(
                connection,
                simulations,
                _new_simulation_values(new_simulation),
                simulations.c.legacy_simulation_id,
            )
        return SimulationRecord.from_row(row)

    def get(self, simulation_id: UUID) -> SimulationRecord | None:
        """Return the simulation with this ID, or None."""
        return self._one(simulations.c.id == simulation_id)

    def get_by_legacy_simulation_id(
        self, legacy_simulation_id: int
    ) -> SimulationRecord | None:
        """Return the simulation migrated from this v1 simulation, or None."""
        return self._one(simulations.c.legacy_simulation_id == legacy_simulation_id)

    def _one(self, condition: ColumnElement[bool]) -> SimulationRecord | None:
        with self.engine.connect() as connection:
            row = (
                connection.execute(select(simulations).where(condition))
                .mappings()
                .first()
            )
        return None if row is None else SimulationRecord.from_row(row)
