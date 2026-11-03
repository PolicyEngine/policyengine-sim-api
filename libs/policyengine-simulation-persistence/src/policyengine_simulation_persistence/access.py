"""The privileges the simulation service's role needs on the Public API's tables.

The Public API's privilege manifest grants the role these privileges, and
its migration chain creates the tables. The readiness check reports any the
connected role lacks, which shows that the deployment's database URL and the
role's grants both reached the running service.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING, Final

from sqlalchemy import String, column, false, func, select, values

if TYPE_CHECKING:
    from collections.abc import Mapping

    from sqlalchemy import Connection

TABLE_SCHEMA: Final = "public"
_CREATE: Final = frozenset({"SELECT", "INSERT"})
_READ: Final = frozenset({"SELECT"})

# The service creates simulations, reports, their runs and household jobs, and
# reads the policy, household and catalog rows a run needs.
SERVICE_TABLE_PRIVILEGES: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        "simulations": _CREATE,
        "reports": _CREATE,
        "simulation_runs": _CREATE,
        "report_runs": _CREATE,
        "report_run_simulation_runs": _CREATE,
        "household_jobs": _CREATE,
        "policies": _READ,
        "households": _READ,
        "tax_benefit_models": _READ,
        "tax_benefit_model_versions": _READ,
        "regions": _READ,
        "datasets": _READ,
        "parameters": _READ,
        "parameter_values": _READ,
    }
)


def missing_table_privileges(connection: Connection) -> list[str]:
    """Return each required privilege the connected role lacks, ordered by table.

    Each is named as ``"<PRIVILEGE> on <table>"``. A table missing from the
    ``public`` schema lacks every privilege the service needs on it.
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
    # to_regclass returns null for a missing table, and has_table_privilege
    # returns null for a null table, so a missing table counts as not granted.
    granted = func.coalesce(
        func.has_table_privilege(
            func.to_regclass(func.concat(f"{TABLE_SCHEMA}.", required.c.table_name)),
            required.c.privilege,
        ),
        false(),
    )
    rows = connection.execute(
        select(required.c.table_name, required.c.privilege)
        .where(~granted)
        .order_by(required.c.table_name, required.c.privilege)
    )
    return [f"{privilege} on {table_name}" for table_name, privilege in rows]
