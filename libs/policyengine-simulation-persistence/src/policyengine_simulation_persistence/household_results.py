"""A computed household, stored as one JSON value on its simulation run.

A household calculation returns a nested document, not a set of figures, so
``household_outputs`` keeps it whole, as PostgreSQL ``json``: the text is kept
as written, so a reader gets back the same keys in the same order and the
same numbers. ``HouseholdOutput`` checks the shape this table can hold (a JSON
object and its schema version); the household functions' own result model
(item 1.11) is validated before it reaches here.

Next to it are the calculation's warnings, in order, and three documents
(``jsonb``, whose key order does not matter): the complete SPM selection the
calculation applied (``spm_config``), the SPM calculation receipt
(``spm_provenance``), and the PolicyEngine.py bundle it ran with
(``bundle``). Each is null when it does not apply, such as SPM for a UK
household.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, JsonValue
from sqlalchemy import (
    JSON,
    CheckConstraint,
    Column,
    ForeignKeyConstraint,
    Integer,
    Table,
    Uuid,
    insert,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from policyengine_simulation_persistence.runs import simulation_runs
from policyengine_simulation_persistence.schema import simulation_metadata
from policyengine_simulation_persistence.simulations import SimulationKind

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy import Connection

    from policyengine_simulation_persistence.runs import SimulationRunRecord


household_outputs = Table(
    "household_outputs",
    simulation_metadata,
    Column("simulation_run_id", Uuid(as_uuid=True), primary_key=True),
    Column("household", JSON(none_as_null=True), nullable=False),
    Column("schema_version", Integer, nullable=False),
    Column("warnings", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    Column("spm_config", JSONB(none_as_null=True), nullable=True),
    Column("spm_provenance", JSONB(none_as_null=True), nullable=True),
    Column("bundle", JSONB(none_as_null=True), nullable=True),
    CheckConstraint("json_typeof(household) = 'object'", name="household"),
    CheckConstraint("schema_version >= 1", name="schema_version"),
    CheckConstraint("jsonb_typeof(warnings) = 'array'", name="warnings"),
    *(
        CheckConstraint(
            f"{document} IS NULL OR jsonb_typeof({document}) = 'object'",
            name=document,
        )
        for document in ("spm_config", "spm_provenance", "bundle")
    ),
    ForeignKeyConstraint(
        ["simulation_run_id"],
        [simulation_runs.c.id],
        name="fk_household_outputs_simulation_run",
        ondelete="RESTRICT",
    ),
)


class HouseholdOutput(BaseModel):
    """A computed household, the version of its document's schema, and its notes.

    ``warnings`` lists, in order, what the calculation reported about its
    inputs; ``spm_config``, ``spm_provenance`` and ``bundle`` are the applied
    SPM selection, the SPM receipt and the PolicyEngine.py bundle, each None
    when it does not apply.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(ge=1)
    household: dict[str, JsonValue]
    warnings: tuple[str, ...] = ()
    spm_config: dict[str, JsonValue] | None = None
    spm_provenance: dict[str, JsonValue] | None = None
    bundle: dict[str, JsonValue] | None = None


def write_household_output(
    connection: Connection, run: SimulationRunRecord, output: HouseholdOutput
) -> None:
    """Store a household run's computed household in the caller's transaction.

    Raise ValueError for a run that is not a household run, or a household
    holding a value JSON cannot represent (NaN or an infinity).
    """
    if run.simulation_kind is not SimulationKind.HOUSEHOLD:
        msg = "only a household simulation run has a computed household"
        raise ValueError(msg)
    try:
        json.dumps(
            [
                output.household,
                output.spm_config,
                output.spm_provenance,
                output.bundle,
            ],
            allow_nan=False,
        )
    except ValueError:
        msg = "a computed household may not hold NaN or an infinity"
        raise ValueError(msg) from None
    connection.execute(
        insert(household_outputs).values(
            simulation_run_id=run.id,
            household=output.household,
            schema_version=output.schema_version,
            warnings=list(output.warnings),
            spm_config=output.spm_config,
            spm_provenance=output.spm_provenance,
            bundle=output.bundle,
        )
    )


def read_household_output(
    connection: Connection, simulation_run_id: UUID
) -> HouseholdOutput:
    """Return a run's stored household; raise LookupError when it has none."""
    row = (
        connection.execute(
            select(household_outputs).where(
                household_outputs.c.simulation_run_id == simulation_run_id
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        msg = f"simulation run {simulation_run_id} has no computed household"
        raise LookupError(msg)
    return HouseholdOutput(
        schema_version=row["schema_version"],
        household=row["household"],
        warnings=tuple(row["warnings"]),
        spm_config=row["spm_config"],
        spm_provenance=row["spm_provenance"],
        bundle=row["bundle"],
    )
