"""The privileges sim's two roles need on the Public API's tables.

The Public API's privilege manifest grants each role these privileges, and
its migration chain creates the tables. The simulation service's readiness
check reports any its role lacks, which shows that the deployment's database
URL and the role's grants both reached the running service. The v2 Modal
calls' role, ``policyengine_v2_modal_worker``, has lists of its own, which
CI checks against the manifest's grants.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING, Final

from sqlalchemy import String, Subquery, and_, false, func, literal, select
from sqlalchemy.dialects.postgresql import ARRAY

from policyengine_simulation_persistence.catalog_tables import (
    PgAttribute,
    PgClass,
    PgNamespace,
)
from policyengine_simulation_persistence.engine import bounded_session
from policyengine_simulation_persistence.orm_base import TABLE_SCHEMA
from policyengine_simulation_persistence.result_tables import RESULT_TABLE_NAMES

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from sqlalchemy.orm import Session, sessionmaker
    from sqlalchemy.sql.selectable import TableValuedAlias

_CREATE: Final = frozenset({"SELECT", "INSERT"})
_READ: Final = frozenset({"SELECT"})

# The service creates simulations, reports, their runs, budget windows'
# calculations, household jobs and simulation runs' cliff calculations, and
# reads the policy, dynamic, household and catalog rows a run needs.
SERVICE_TABLE_PRIVILEGES: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        "simulations": _CREATE,
        "reports": _CREATE,
        "simulation_runs": _CREATE,
        "report_runs": _CREATE,
        "budget_window_runs": _CREATE,
        "household_jobs": _CREATE,
        "cliff_impacts": _CREATE,
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
# The run state the service records for a run whose Modal call it starts: the
# call's ID once Modal accepts the call, or the failure when Modal refuses it.
_CALL_START: Final = frozenset(
    {"status", "modal_call_id", "error_message", "completed_at", "updated_at"}
)
# The state the service records for a budget-window run, which has no Modal
# call of its own: the failure when Modal refuses one of its first years.
_WINDOW_FAILURE: Final = frozenset(
    {"status", "error_message", "completed_at", "updated_at"}
)
# The columns the service's role may UPDATE, by table. It holds UPDATE on
# these columns alone, never on a whole table, so the database refuses any
# other change to a run.
SERVICE_UPDATABLE_COLUMNS: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        "simulation_runs": _CALL_START,
        "report_runs": _CALL_START,
        "household_jobs": _CALL_START,
        "budget_window_runs": _WINDOW_FAILURE,
        "cliff_impacts": _CALL_START,
    }
)

# A run's state, which the Modal call holding it records: its claim, its
# outcome and when each happened.
_RUN_STATE: Final = frozenset(
    {
        "status",
        "modal_call_id",
        "error_message",
        "started_at",
        "completed_at",
        "updated_at",
    }
)
# The v2 Modal coordinators, workers, household functions and stuck-run sweep
# read the runs and cliff calculations they were given and insert their results,
# once each. A session's insert of a result also reads the row's server defaults
# back. A cliff calculation's result is in its own row.
MODAL_WORKER_TABLE_PRIVILEGES: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        "report_runs": _READ,
        "simulation_runs": _READ,
        "household_jobs": _READ,
        "budget_window_runs": _READ,
        "cliff_impacts": _READ,
        **dict.fromkeys(RESULT_TABLE_NAMES, _CREATE),
    }
)
# The columns the Modal worker role may UPDATE, by table: each run's and cliff
# calculation's state and the results its own row holds. A budget-window run
# has no Modal call of its own; its years' coordinators record its status,
# failure and times.
MODAL_WORKER_UPDATABLE_COLUMNS: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        # A report run's cliff impact is its simulation runs' cliff results,
        # so the role never writes its four cliff columns.
        "report_runs": _RUN_STATE | {"spm_provenance", "data_version"},
        "simulation_runs": _RUN_STATE
        | {"output_uri", "output_sha256", "household_result"},
        "household_jobs": _RUN_STATE | {"result"},
        "budget_window_runs": _RUN_STATE - {"modal_call_id"},
        "cliff_impacts": _RUN_STATE | {"cliff_gap", "cliff_share"},
    }
)


def _public_tables() -> Subquery:
    """Select the oid and name of each table in the ``public`` schema."""
    return (
        select(PgClass.oid, PgClass.relname)
        .join(PgNamespace, PgNamespace.oid == PgClass.relnamespace)
        .where(
            PgNamespace.nspname == TABLE_SCHEMA,
            PgClass.relkind.in_(["r", "p"]),
        )
        .subquery("public_tables")
    )


def _required_rows(
    pairs: Sequence[tuple[str, str]],
    *,
    name: str,
    first_column: str,
    second_column: str,
) -> TableValuedAlias:
    """Return ``pairs`` as a table of two text columns, one row for each pair."""
    return (
        func.unnest(
            literal([first for first, _ in pairs], ARRAY(String)),
            literal([second for _, second in pairs], ARRAY(String)),
        )
        .table_valued(first_column, second_column)
        .render_derived(name=name)
    )


def _missing_table_grants(
    session: Session, table_privileges: Mapping[str, frozenset[str]]
) -> list[tuple[str, str, str]]:
    """Return ``(table, privilege, "")`` for each table privilege the role lacks."""
    required = _required_rows(
        [
            (table_name, privilege)
            for table_name, privileges in table_privileges.items()
            for privilege in sorted(privileges)
        ],
        name="required_privileges",
        first_column="table_name",
        second_column="privilege",
    )
    public_tables = _public_tables()
    # A missing table joins to a null oid, and has_table_privilege returns
    # null for a null table, so a missing table counts as not granted.
    granted = func.coalesce(
        func.has_table_privilege(public_tables.c.oid, required.c.privilege),
        false(),
    )
    rows = session.execute(
        select(required.c.table_name, required.c.privilege)
        .select_from(required)
        .outerjoin(public_tables, public_tables.c.relname == required.c.table_name)
        .where(~granted)
    )
    return [(table_name, privilege, "") for table_name, privilege in rows]


def _missing_column_grants(
    session: Session, updatable_columns: Mapping[str, frozenset[str]]
) -> list[tuple[str, str, str]]:
    """Return ``(table, "UPDATE", column)`` for each column the role can't update."""
    required = _required_rows(
        [
            (table_name, column_name)
            for table_name, column_names in updatable_columns.items()
            for column_name in sorted(column_names)
        ],
        name="required_column_updates",
        first_column="table_name",
        second_column="column_name",
    )
    public_tables = _public_tables()
    # A missing table or column joins to a null column number, which
    # has_column_privilege answers with null, so it counts as not granted.
    # Asked by number, not name: by name, a missing column raises an error.
    granted = func.coalesce(
        func.has_column_privilege(public_tables.c.oid, PgAttribute.attnum, "UPDATE"),
        false(),
    )
    rows = session.execute(
        select(required.c.table_name, required.c.column_name)
        .select_from(required)
        .outerjoin(public_tables, public_tables.c.relname == required.c.table_name)
        .outerjoin(
            PgAttribute,
            and_(
                PgAttribute.attrelid == public_tables.c.oid,
                PgAttribute.attname == required.c.column_name,
                PgAttribute.attnum > 0,
                PgAttribute.attisdropped.is_(false()),
            ),
        )
        .where(~granted)
    )
    return [(table_name, "UPDATE", column_name) for table_name, column_name in rows]


def missing_table_privileges(
    session: Session,
    *,
    table_privileges: Mapping[str, frozenset[str]] = SERVICE_TABLE_PRIVILEGES,
    updatable_columns: Mapping[str, frozenset[str]] = SERVICE_UPDATABLE_COLUMNS,
) -> list[str]:
    """Return each required privilege the session's role lacks, ordered by table.

    The required privileges are the simulation service's unless another
    role's lists are given, such as ``MODAL_WORKER_TABLE_PRIVILEGES`` and
    ``MODAL_WORKER_UPDATABLE_COLUMNS``. A table privilege is named as
    ``"<PRIVILEGE> on <table>"`` and a column privilege as
    ``"UPDATE (<column>) on <table>"``. A table missing from the ``public``
    schema lacks every privilege required on it, and a missing column lacks
    its own. A role without the USAGE privilege on that schema can't use any
    of its grants, so ``"USAGE on schema public"`` comes first.
    """
    missing_grants = sorted(
        [
            *_missing_table_grants(session, table_privileges),
            *_missing_column_grants(session, updatable_columns),
        ]
    )
    missing = [
        f"{privilege} ({column_name}) on {table_name}"
        if column_name
        else f"{privilege} on {table_name}"
        for table_name, privilege, column_name in missing_grants
    ]
    schema_usage = session.execute(
        select(func.has_schema_privilege(TABLE_SCHEMA, "USAGE"))
    ).scalar_one()
    if not schema_usage:
        missing.insert(0, f"USAGE on schema {TABLE_SCHEMA}")
    return missing


def read_missing_table_privileges(
    session_factory: sessionmaker[Session], *, statement_timeout_milliseconds: int
) -> list[str]:
    """Run ``missing_table_privileges`` in a transaction the database bounds.

    ``statement_timeout`` is set with ``set_config`` local to the transaction,
    so it works through Supabase's transaction pooler and never stays on the
    pooled connection; a statement that runs longer raises the database's
    cancellation as an OperationalError.
    """
    with bounded_session(
        session_factory,
        statement_timeout_milliseconds=statement_timeout_milliseconds,
    ) as session:
        return missing_table_privileges(session)
