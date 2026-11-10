"""Read a stored policy as the reform values a calculation applies.

The Public API stores a policy's parameter values against the parameters of
the catalog version it was saved on, each from a start date until an end
date, or with no end date indefinitely. A calculation on any catalog version
applies them by parameter name, as ``{parameter name: {period: value}}``, the
shape of the Public API's policy JSON that the household functions' contract
and the economy requests take.

Each period is a range, ``YYYY-MM-DD.YYYY-MM-DD``, with the dates read in
UTC. A value with no end date applies to the end of 2200, the latest year a
household job or simulation run can be for. A range is used even then
because the countries' models read a start date alone differently:
policyengine-core reads it as "from that day on", but policyengine-uk's
reform dictionary as that one day.
"""

from __future__ import annotations

from datetime import UTC, date
from typing import TYPE_CHECKING, Final

from sqlalchemy import select

from policyengine_simulation_persistence.input_tables import (
    parameter_values,
    parameters,
    policies,
)
from policyengine_simulation_persistence.tables import LATEST_YEAR

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime
    from uuid import UUID

    from sqlalchemy import Connection

# The last day a value without an end date is applied.
OPEN_ENDED_STOP: Final = date(LATEST_YEAR, 12, 31)

# A policy's reform values: each parameter's values by period.
type PolicyReform = dict[str, dict[str, object]]


class UnknownPolicyError(LookupError):
    """The Public API has no such policy in the country."""


class MissingPolicyParametersError(ValueError):
    """The policy sets parameters the calculation's catalog version lacks."""

    def __init__(self, parameter_names: Sequence[str]) -> None:
        """Name every parameter the catalog version lacks, in name order."""
        self.parameter_names = tuple(parameter_names)
        super().__init__(
            "the policy sets parameters the catalog version does not have: "
            + ", ".join(self.parameter_names)
        )


class ConflictingPolicyValuesError(ValueError):
    """Two of a policy's values for one parameter fall on the same period."""


def reform_period(start_date: datetime, end_date: datetime | None) -> str:
    """Return a stored value's period as the range a calculation applies.

    The dates are read in UTC. A value without an end date applies to the
    end of 2200; one that starts later than that ends where it starts.
    """
    start_day = start_date.astimezone(UTC).date()
    stop_day = (
        max(start_day, OPEN_ENDED_STOP)
        if end_date is None
        else end_date.astimezone(UTC).date()
    )
    return f"{start_day.isoformat()}.{stop_day.isoformat()}"


def read_policy_reform(
    connection: Connection,
    *,
    country: str,
    policy_id: UUID,
    catalog_version_id: UUID,
) -> PolicyReform:
    """Return the values ``country``'s policy sets, by parameter name and period.

    Raise UnknownPolicyError when the country has no such policy,
    MissingPolicyParametersError when ``catalog_version_id`` lacks a
    parameter the policy sets, and ConflictingPolicyValuesError when two
    values of one parameter fall on the same period.
    """
    found = connection.execute(
        select(policies.c.id).where(
            policies.c.id == policy_id, policies.c.country_id == country
        )
    ).scalar_one_or_none()
    if found is None:
        msg = f"{country} has no policy {policy_id}"
        raise UnknownPolicyError(msg)
    values = connection.execute(
        select(
            parameters.c.name,
            parameter_values.c.value_json,
            parameter_values.c.start_date,
            parameter_values.c.end_date,
        )
        .join(parameters, parameters.c.id == parameter_values.c.parameter_id)
        .where(parameter_values.c.policy_id == policy_id)
        .order_by(parameters.c.name, parameter_values.c.start_date)
    ).all()
    names = sorted({row.name for row in values})
    available = set(
        connection.execute(
            select(parameters.c.name).where(
                parameters.c.tax_benefit_model_version_id == catalog_version_id,
                parameters.c.name.in_(names),
            )
        ).scalars()
    )
    missing = [name for name in names if name not in available]
    if missing:
        raise MissingPolicyParametersError(missing)
    reform: PolicyReform = {}
    for row in values:
        periods = reform.setdefault(row.name, {})
        period = reform_period(row.start_date, row.end_date)
        if period in periods:
            msg = f"the policy sets {row.name} twice for {period}"
            raise ConflictingPolicyValuesError(msg)
        periods[period] = row.value_json
    return reform
