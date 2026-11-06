"""The privileges the simulation service's role needs on the Public API's tables.

The Public API's privilege manifest grants the role these privileges, and
its migration chain creates the tables. The readiness check reports any the
connected role lacks, which shows that the deployment's database URL and the
role's grants both reached the running service.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING, Final

from sqlalchemy import String, and_, column, false, func, select, table, values

from policyengine_simulation_persistence.tables import TABLE_SCHEMA

if TYPE_CHECKING:
    from collections.abc import Mapping

    from sqlalchemy import Connection, Engine

# PostgreSQL's catalog, which any role may read: a table is found by name
# without the USAGE privilege on its schema that to_regclass would need.
_pg_class = table(
    "pg_class",
    column("oid"),
    column("relname"),
    column("relnamespace"),
    column("relkind"),
    schema="pg_catalog",
)
_pg_namespace = table(
    "pg_namespace", column("oid"), column("nspname"), schema="pg_catalog"
)
_CREATE: Final = frozenset({"SELECT", "INSERT"})
_READ: Final = frozenset({"SELECT"})

# The service creates simulations, reports, their runs, budget windows'
# calculations and household jobs, and reads the policy, dynamic, household
# and catalog rows a run needs.
SERVICE_TABLE_PRIVILEGES: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        "simulations": _CREATE,
        "reports": _CREATE,
        "simulation_runs": _CREATE,
        "report_runs": _CREATE,
        "budget_window_runs": _CREATE,
        "household_jobs": _CREATE,
        "policies": _READ,
        "households": _READ,
        "tax_benefit_models": _READ,
        "tax_benefit_model_versions": _READ,
        "regions": _READ,
        "datasets": _READ,
        "parameters": _READ,
        "parameter_values": _READ,
        "dynamics": _READ,
    }
)


def missing_table_privileges(connection: Connection) -> list[str]:
    """Return each required privilege the connected role lacks, ordered by table.

    Each is named as ``"<PRIVILEGE> on <table>"``. A table missing from the
    ``public`` schema lacks every privilege the service needs on it. A role
    without the USAGE privilege on that schema can't use any table grant, so
    ``"USAGE on schema public"`` comes first.
    """
    required = values(
        column("table_name", String),
        column("privilege", String),
        name="required_privileges",
    ).data(
        [
            (table_name, privilege)
            for table_name, privileges in SERVICE_TABLE_PRIVILEGES.items()
            for privilege in sorted(privileges)
        ]
    )
    public_tables = (
        select(_pg_class.c.oid, _pg_class.c.relname)
        .join(_pg_namespace, _pg_namespace.c.oid == _pg_class.c.relnamespace)
        .where(
            and_(
                _pg_namespace.c.nspname == TABLE_SCHEMA,
                _pg_class.c.relkind.in_(["r", "p"]),
            )
        )
        .subquery("public_tables")
    )
    # A missing table joins to a null oid, and has_table_privilege returns
    # null for a null table, so a missing table counts as not granted.
    granted = func.coalesce(
        func.has_table_privilege(public_tables.c.oid, required.c.privilege),
        false(),
    )
    rows = connection.execute(
        select(required.c.table_name, required.c.privilege)
        .select_from(
            required.outerjoin(
                public_tables, public_tables.c.relname == required.c.table_name
            )
        )
        .where(~granted)
        .order_by(required.c.table_name, required.c.privilege)
    )
    missing = [f"{privilege} on {table_name}" for table_name, privilege in rows]
    schema_usage = connection.execute(
        select(func.has_schema_privilege(TABLE_SCHEMA, "USAGE"))
    ).scalar_one()
    if not schema_usage:
        missing.insert(0, f"USAGE on schema {TABLE_SCHEMA}")
    return missing


def read_missing_table_privileges(
    engine: Engine, *, statement_timeout_milliseconds: int
) -> list[str]:
    """Run ``missing_table_privileges`` in a transaction the database bounds.

    ``statement_timeout`` is set with ``set_config`` local to the transaction,
    so it works through Supabase's transaction pooler and never stays on the
    pooled connection; a statement that runs longer raises the database's
    cancellation as an OperationalError.
    """
    with engine.connect() as connection, connection.begin():
        connection.execute(
            select(
                func.set_config(
                    "statement_timeout",
                    str(statement_timeout_milliseconds),
                    True,  # noqa: FBT003  # set_config's is_local argument
                )
            )
        )
        return missing_table_privileges(connection)
