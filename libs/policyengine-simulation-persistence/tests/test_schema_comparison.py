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
    foreign_key_targets,
    foreign_keys_to,
    read_column_differences,
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


def test_each_foreign_key_is_read_with_the_table_it_names() -> None:
    """PostgreSQL qualifies a table outside the search path and quotes odd names."""
    schema = _simulation_runs(
        constraints={
            "fk_simulation_runs_dataset_id_datasets": (
                "FOREIGN KEY (dataset_id) REFERENCES datasets(id) ON DELETE RESTRICT"
            ),
            "fk_simulation_runs_user_id_users": (
                "FOREIGN KEY (user_id) REFERENCES auth.users(id)"
            ),
            "fk_simulation_runs_owner_id_owners": (
                'FOREIGN KEY (owner_id) REFERENCES "Owners"(id) ON DELETE SET NULL'
            ),
            "fk_simulation_runs_window": (
                "FOREIGN KEY (window_id, simulation_id) REFERENCES "
                "budget_window_runs(id, report_id) ON DELETE CASCADE"
            ),
            "ck_simulation_runs_year": "CHECK (((year >= 1900) AND (year <= 2200)))",
        }
    )

    assert foreign_key_targets(schema) == {
        ("simulation_runs", "fk_simulation_runs_simulation_id_simulations"): (
            "simulations"
        ),
        ("simulation_runs", "fk_simulation_runs_dataset_id_datasets"): "datasets",
        ("simulation_runs", "fk_simulation_runs_user_id_users"): "auth.users",
        ("simulation_runs", "fk_simulation_runs_owner_id_owners"): "Owners",
        ("simulation_runs", "fk_simulation_runs_window"): "budget_window_runs",
    }


def test_quoted_names_are_read_whole() -> None:
    """A quoted name may hold a dot, a parenthesis or a doubled quote."""
    schema = _simulation_runs(
        constraints={
            "fk_simulation_runs_owner": (
                'FOREIGN KEY (owner_id) REFERENCES "Auth"."Users"(id)'
            ),
            "fk_simulation_runs_odd": (
                'FOREIGN KEY (odd_id) REFERENCES "odd.name(1)"(id)'
            ),
            "fk_simulation_runs_quote": (
                'FOREIGN KEY ("quote id") REFERENCES "say ""hi"""(id)'
            ),
        }
    )

    targets = foreign_key_targets(schema)

    assert targets[("simulation_runs", "fk_simulation_runs_owner")] == "Auth.Users"
    assert targets[("simulation_runs", "fk_simulation_runs_odd")] == "odd.name(1)"
    assert targets[("simulation_runs", "fk_simulation_runs_quote")] == 'say "hi"'


def test_keys_to_the_given_tables_are_found_on_every_table() -> None:
    """A key's name is unique only within its table, so each is kept by both."""
    simulation_runs = _simulation_runs(
        constraints={
            "fk_policy": "FOREIGN KEY (policy_id) REFERENCES policies(id)",
            "fk_user": "FOREIGN KEY (user_id) REFERENCES auth.users(id)",
        }
    )
    household_jobs = TableShape(
        columns={"id": ColumnShape("uuid", nullable=False, default=None)},
        constraints={
            "pk_household_jobs": "PRIMARY KEY (id)",
            "fk_policy": (
                "FOREIGN KEY (policy_id) REFERENCES policies(id) ON DELETE SET NULL"
            ),
        },
        indexes={},
    )
    schema = SchemaShape(
        tables={**simulation_runs.tables, "household_jobs": household_jobs},
        enums=simulation_runs.enums,
    )

    # auth.users is another schema's table, and simulations isn't asked for.
    assert foreign_keys_to(schema, {"policies", "users"}) == {
        ("simulation_runs", "fk_policy"): "policies",
        ("household_jobs", "fk_policy"): "policies",
    }


def test_a_foreign_key_whose_table_cannot_be_read_is_refused() -> None:
    """A key left uncounted would hide from a check of which keys exist."""
    schema = _simulation_runs(
        constraints={"fk_simulation_runs_unreadable": "FOREIGN KEY (x) REFERENCES"}
    )

    with pytest.raises(ValueError, match="fk_simulation_runs_unreadable"):
        foreign_key_targets(schema)


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


def test_an_index_only_the_model_has_is_reported() -> None:
    real = _simulation_runs(without_indexes=["ix_simulation_runs_status_started_at"])

    assert schema_differences(_simulation_runs(), real) == [
        "simulation_runs: the Public API's table has no index "
        "ix_simulation_runs_status_started_at (CREATE INDEX "
        "ix_simulation_runs_status_started_at ON public.simulation_runs USING "
        "btree (status, created_at))",
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


def _households(**column_changes: ColumnShape) -> SchemaShape:
    """Return a households table as the Public API builds it, changed as given."""
    columns = {
        "id": ColumnShape("uuid", nullable=False, default=None),
        "created_at": ColumnShape(
            "timestamp with time zone", nullable=False, default="now()"
        ),
        "household_data": ColumnShape("jsonb", nullable=False, default=None),
        "default_year": ColumnShape("integer", nullable=True, default=None),
        **column_changes,
    }
    return SchemaShape(
        tables={
            "households": TableShape(
                columns=columns,
                constraints={"pk_households": "PRIMARY KEY (id)"},
                indexes={},
            )
        },
        enums={},
    )


def test_input_models_may_leave_out_columns_defaults_and_constraints() -> None:
    """A read-only model names only the columns the service reads."""
    real = _households()
    models = SchemaShape(
        tables={
            "households": TableShape(
                columns={
                    "id": ColumnShape("uuid", nullable=False, default=None),
                    "default_year": ColumnShape("integer", nullable=True, default=None),
                },
                constraints={},
                indexes={},
            )
        },
        enums={},
    )

    assert read_column_differences(models, real) == []


def test_input_models_differ_where_a_column_they_read_differs() -> None:
    real = _households()
    models = SchemaShape(
        tables={
            **_households(
                default_year=ColumnShape("bigint", nullable=True, default=None),
                household_data=ColumnShape("jsonb", nullable=True, default=None),
                label=ColumnShape(
                    "character varying(255)", nullable=True, default=None
                ),
            ).tables,
            "parameters": TableShape(
                columns={"id": ColumnShape("uuid", nullable=False, default=None)},
                constraints={},
                indexes={},
            ),
        },
        enums={},
    )

    assert read_column_differences(models, real) == [
        "households.household_data: the model allows null, the Public API's refuses it",
        "households.default_year: the model is bigint, the Public API's is integer",
        "households.label: the Public API's table has no such column",
        "parameters: the Public API has no such table",
    ]
