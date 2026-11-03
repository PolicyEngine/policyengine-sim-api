"""How sim's table models are compared with the Public API's tables.

``schema_differences`` lists every way the models' tables differ from the
tables the Public API's migration chain builds. These tests need no database:
they compare descriptions built here, except the one marked ``postgres``,
which describes a table it creates.
"""

from __future__ import annotations

import pytest
from schema_comparison import (
    ColumnShape,
    SchemaShape,
    TableShape,
    describe_tables,
    schema_differences,
    schema_from_json,
    schema_to_json,
)
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

STATUS_ENUM = "v2_simulation_status"


def _simulation_runs(**changes) -> SchemaShape:
    """Return a simulation_runs table and its status enum, changed as given."""
    columns = {
        "id": ColumnShape("uuid", nullable=False, default=None),
        "simulation_id": ColumnShape("uuid", nullable=False, default=None),
        "status": ColumnShape(
            f"enum {STATUS_ENUM}",
            nullable=False,
            default=f"'pending'::{STATUS_ENUM}",
        ),
        "created_at": ColumnShape(
            "timestamp with time zone", nullable=False, default="now()"
        ),
        "modal_call_id": ColumnShape(
            "character varying(255)", nullable=True, default=None
        ),
    }
    columns.update(changes.pop("columns", {}))
    for removed in changes.pop("without_columns", ()):
        del columns[removed]
    constraints = {
        "pk_simulation_runs": "PRIMARY KEY (id)",
        "fk_simulation_runs_simulation_id_simulations": (
            "FOREIGN KEY (simulation_id) REFERENCES simulations(id) ON DELETE CASCADE"
        ),
    }
    constraints.update(changes.pop("constraints", {}))
    for removed in changes.pop("without_constraints", ()):
        del constraints[removed]
    indexes = {
        "ix_simulation_runs_status_started_at": (
            "CREATE INDEX ix_simulation_runs_status_started_at ON "
            "public.simulation_runs USING btree (status, created_at)"
        ),
    }
    indexes.update(changes.pop("indexes", {}))
    for removed in changes.pop("without_indexes", ()):
        del indexes[removed]
    enums = changes.pop(
        "enums", {STATUS_ENUM: ("pending", "running", "succeeded", "failed")}
    )
    assert not changes, changes
    return SchemaShape(
        tables={
            "simulation_runs": TableShape(
                columns=columns, constraints=constraints, indexes=indexes
            )
        },
        enums=enums,
    )


def test_identical_tables_have_no_differences() -> None:
    assert schema_differences(_simulation_runs(), _simulation_runs()) == []


@pytest.mark.parametrize(
    ("models", "real", "difference"),
    [
        (
            _simulation_runs(
                columns={
                    "status": ColumnShape(
                        "character varying(32)",
                        nullable=False,
                        default=f"'pending'::{STATUS_ENUM}",
                    )
                }
            ),
            _simulation_runs(),
            "simulation_runs.status: the model is character varying(32), the "
            "Public API's is enum v2_simulation_status",
        ),
        (
            _simulation_runs(
                columns={
                    "modal_call_id": ColumnShape(
                        "character varying(255)", nullable=False, default=None
                    )
                }
            ),
            _simulation_runs(),
            "simulation_runs.modal_call_id: the model refuses null, the Public "
            "API's allows it",
        ),
        (
            _simulation_runs(
                columns={
                    "created_at": ColumnShape(
                        "timestamp with time zone", nullable=False, default=None
                    )
                }
            ),
            _simulation_runs(),
            "simulation_runs.created_at: the model has no default, the Public "
            "API's defaults to now()",
        ),
        (
            _simulation_runs(without_columns=["modal_call_id"]),
            _simulation_runs(),
            "simulation_runs.modal_call_id: the model has no such column",
        ),
        (
            _simulation_runs(),
            _simulation_runs(without_columns=["modal_call_id"]),
            "simulation_runs.modal_call_id: the Public API's table has no such column",
        ),
        (
            _simulation_runs(
                constraints={
                    "fk_simulation_runs_simulation_id_simulations": (
                        "FOREIGN KEY (simulation_id) REFERENCES simulations(id)"
                    )
                }
            ),
            _simulation_runs(),
            "simulation_runs: fk_simulation_runs_simulation_id_simulations is "
            "FOREIGN KEY (simulation_id) REFERENCES simulations(id) in the "
            "model, FOREIGN KEY (simulation_id) REFERENCES simulations(id) ON "
            "DELETE CASCADE in the Public API",
        ),
        (
            _simulation_runs(
                without_constraints=["fk_simulation_runs_simulation_id_simulations"]
            ),
            _simulation_runs(),
            "simulation_runs: the model has no constraint "
            "fk_simulation_runs_simulation_id_simulations (FOREIGN KEY "
            "(simulation_id) REFERENCES simulations(id) ON DELETE CASCADE)",
        ),
        (
            _simulation_runs(),
            _simulation_runs(
                without_constraints=["fk_simulation_runs_simulation_id_simulations"]
            ),
            "simulation_runs: the Public API's table has no constraint "
            "fk_simulation_runs_simulation_id_simulations (FOREIGN KEY "
            "(simulation_id) REFERENCES simulations(id) ON DELETE CASCADE)",
        ),
        (
            _simulation_runs(without_indexes=["ix_simulation_runs_status_started_at"]),
            _simulation_runs(),
            "simulation_runs: the model has no index "
            "ix_simulation_runs_status_started_at (CREATE INDEX "
            "ix_simulation_runs_status_started_at ON public.simulation_runs USING "
            "btree (status, created_at))",
        ),
        (
            _simulation_runs(),
            _simulation_runs(enums={STATUS_ENUM: ("pending", "running", "failed")}),
            "enum v2_simulation_status: the model has (pending, running, "
            "succeeded, failed), the Public API has (pending, running, failed)",
        ),
        (
            _simulation_runs(),
            _simulation_runs(enums={}),
            "enum v2_simulation_status: the Public API has no such enum in its "
            "public schema",
        ),
        (
            _simulation_runs(),
            SchemaShape(tables={}, enums=_simulation_runs().enums),
            "simulation_runs: the Public API has no such table",
        ),
    ],
    ids=[
        "type",
        "nullability",
        "default",
        "column-only-in-the-public-api",
        "column-only-in-the-model",
        "constraint-definition",
        "constraint-only-in-the-public-api",
        "constraint-only-in-the-model",
        "index-only-in-the-public-api",
        "enum-values",
        "missing-enum",
        "missing-table",
    ],
)
def test_each_difference_is_reported(models, real, difference) -> None:
    assert schema_differences(models, real) == [difference]


def test_an_index_with_another_definition_is_reported() -> None:
    reordered = (
        "CREATE INDEX ix_simulation_runs_status_started_at ON "
        "public.simulation_runs USING btree (created_at, status)"
    )
    models = _simulation_runs(
        indexes={"ix_simulation_runs_status_started_at": reordered}
    )

    assert schema_differences(models, _simulation_runs()) == [
        f"simulation_runs: ix_simulation_runs_status_started_at is {reordered} in "
        "the model, CREATE INDEX ix_simulation_runs_status_started_at ON "
        "public.simulation_runs USING btree (status, created_at) in the Public API",
    ]


def test_tables_the_models_leave_out_are_not_compared() -> None:
    real = _simulation_runs()
    real.tables["users"] = TableShape(
        columns={"id": ColumnShape("uuid", nullable=False, default=None)},
        constraints={},
        indexes={},
    )

    assert schema_differences(_simulation_runs(), real) == []


@pytest.mark.postgres
def test_the_catalog_describes_each_part_of_a_table(disposable_database) -> None:
    engine = create_engine(
        make_url(disposable_database.superuser_url).set(
            drivername="postgresql+psycopg"
        ),
        isolation_level="AUTOCOMMIT",
    )
    statements = [
        "CREATE TYPE v2_simulation_status AS ENUM ('pending', 'running')",
        "CREATE TABLE simulations (id uuid CONSTRAINT pk_simulations PRIMARY KEY)",
        "CREATE TABLE simulation_runs ("
        "id uuid CONSTRAINT pk_simulation_runs PRIMARY KEY, "
        "simulation_id uuid NOT NULL CONSTRAINT "
        "fk_simulation_runs_simulation_id_simulations REFERENCES simulations (id) "
        "ON DELETE CASCADE, "
        "status v2_simulation_status NOT NULL DEFAULT 'pending', "
        "attempt integer NOT NULL CONSTRAINT ck_simulation_runs_attempt "
        "CHECK (attempt > 0), "
        "created_at timestamp with time zone NOT NULL DEFAULT now(), "
        "modal_call_id varchar(255), "
        "CONSTRAINT uq_simulation_runs_simulation_attempt "
        "UNIQUE (simulation_id, attempt))",
        "CREATE INDEX ix_simulation_runs_status_created_at "
        "ON simulation_runs (status, created_at)",
    ]
    try:
        with engine.connect() as connection:
            for statement in statements:
                connection.execute(text(statement))
            described = describe_tables(connection, ["simulation_runs", "absent"])
    finally:
        engine.dispose()

    assert described == SchemaShape(
        tables={
            "simulation_runs": TableShape(
                columns={
                    "id": ColumnShape("uuid", nullable=False, default=None),
                    "simulation_id": ColumnShape("uuid", nullable=False, default=None),
                    "status": ColumnShape(
                        "enum v2_simulation_status",
                        nullable=False,
                        default="'pending'::v2_simulation_status",
                    ),
                    "attempt": ColumnShape("integer", nullable=False, default=None),
                    "created_at": ColumnShape(
                        "timestamp with time zone", nullable=False, default="now()"
                    ),
                    "modal_call_id": ColumnShape(
                        "character varying(255)", nullable=True, default=None
                    ),
                },
                constraints={
                    "ck_simulation_runs_attempt": "CHECK ((attempt > 0))",
                    "fk_simulation_runs_simulation_id_simulations": (
                        "FOREIGN KEY (simulation_id) REFERENCES simulations(id) "
                        "ON DELETE CASCADE"
                    ),
                    "pk_simulation_runs": "PRIMARY KEY (id)",
                    "uq_simulation_runs_simulation_attempt": (
                        "UNIQUE (simulation_id, attempt)"
                    ),
                },
                indexes={
                    "ix_simulation_runs_status_created_at": (
                        "CREATE INDEX ix_simulation_runs_status_created_at ON "
                        "public.simulation_runs USING btree (status, created_at)"
                    ),
                },
            )
        },
        enums={"v2_simulation_status": ("pending", "running")},
    )


def test_a_description_round_trips_through_json(tmp_path) -> None:
    described = _simulation_runs()
    path = tmp_path / "schema.json"
    path.write_text(schema_to_json(described), encoding="utf-8")

    assert schema_from_json(path) == described
