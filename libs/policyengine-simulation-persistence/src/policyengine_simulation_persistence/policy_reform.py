"""Read a stored policy, or dynamic, as the reform values a calculation applies.

The Public API stores a policy's parameter values against the parameters of
the catalog version it was saved on, each from a start date until an end
date, or with no end date indefinitely. A calculation on any catalog version
applies them by parameter name, as ``{parameter name: {period: value}}``, the
shape of the Public API's policy JSON that the household functions' contract
and the economy requests take. A dynamic's values are stored and read the same
way. A simulation applies its policy's and its dynamic's values together, and
is refused when both set the same parameter (``combine_reforms``).

Each period is a range, ``YYYY-MM-DD.YYYY-MM-DD``, with the dates read in
UTC. A value with no end date follows .py's own rule
(``policyengine.utils.parametric_reforms.reform_dict_from_parameter_values``):
it stops the day before the same parameter's next later value starts, and
otherwise runs to the end of 2200, the latest year a household job or
simulation run can be for, where .py stops at 2100. So once a later bounded
value ends, current law applies. The reader gives the same periods, in the
same order, as .py does for the values in start order, except that it
leaves out an open-ended value whose range would end before it starts:
one followed the same day by a later value, or one starting after 2200.
.py gives such a value a range that ends before it starts too.
policyengine-core applies that range on no day, but policyengine-uk refuses
it (its ``interval_bounds`` raises "ends before it starts"), so a UK
calculation given .py's dict fails where one given the reader's runs. A
range is used even for the last value because the countries' models read a
start date alone differently: policyengine-core reads it as "from that day
on", but policyengine-uk's reform dictionary as that one day.
"""

from __future__ import annotations

from datetime import UTC, date, timedelta
from typing import TYPE_CHECKING, Final, Literal

from sqlalchemy import select

from policyengine_simulation_persistence.input_tables import (
    dynamics,
    parameter_values,
    parameters,
    policies,
)
from policyengine_simulation_persistence.tables import LATEST_YEAR

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime
    from uuid import UUID

    from sqlalchemy import ColumnElement, Connection

# The last day a value without an end date is applied.
OPEN_ENDED_STOP: Final = date(LATEST_YEAR, 12, 31)

# A policy's reform values: each parameter's values by period.
type PolicyReform = dict[str, dict[str, object]]
# What stores the values a calculation applies.
type ReformSource = Literal["policy", "dynamic"]


class UnknownPolicyError(LookupError):
    """The Public API has no such policy in the country."""


class UnknownDynamicError(LookupError):
    """The Public API has no such dynamic."""


class MissingPolicyParametersError(ValueError):
    """The policy, or dynamic, sets parameters the calculation's catalog version lacks.

    ``source`` says which of the two sets them.
    """

    def __init__(
        self, parameter_names: Sequence[str], *, source: ReformSource = "policy"
    ) -> None:
        """Name every parameter the catalog version lacks, in name order."""
        self.parameter_names = tuple(parameter_names)
        self.source: ReformSource = source
        super().__init__(
            f"the {source} sets parameters the catalog version does not have: "
            + ", ".join(self.parameter_names)
        )


class OverlappingReformError(ValueError):
    """A simulation's policy and dynamic both set the same parameters.

    Neither is applied over the other, so the simulation cannot be
    calculated.
    """

    def __init__(self, parameter_names: Sequence[str]) -> None:
        """Name every parameter both set, in name order."""
        self.parameter_names = tuple(parameter_names)
        super().__init__(
            "the policy and the dynamic both set: " + ", ".join(self.parameter_names)
        )


def _period(
    start_date: datetime,
    end_date: datetime | None,
    next_start_date: datetime | None,
) -> str | None:
    """Return a value's period by .py's rule, or None when no day is in it.

    The dates are read in UTC. A value without an end date stops the day
    before ``next_start_date``, the start of the same parameter's next later
    value, if any, and otherwise at the end of 2200. Its range would end
    before it starts when that next value starts the same day, or when it
    starts after 2200: then it is None.
    """
    start_day = _utc_day(start_date)
    if end_date is not None:
        return f"{start_day.isoformat()}.{_utc_day(end_date).isoformat()}"
    stop_day = (
        OPEN_ENDED_STOP
        if next_start_date is None
        else _utc_day(next_start_date) - timedelta(days=1)
    )
    if stop_day < start_day:
        return None
    return f"{start_day.isoformat()}.{stop_day.isoformat()}"


def read_policy_reform(
    connection: Connection,
    *,
    country: str,
    policy_id: UUID,
    catalog_version_id: UUID,
) -> PolicyReform:
    """Return the values ``country``'s policy sets, by parameter name and period.

    The periods are .py's for the values in start order (see the module
    docstring). Raise UnknownPolicyError when the country has no such
    policy, and MissingPolicyParametersError when ``catalog_version_id``
    lacks a parameter the policy sets.
    """
    found = connection.execute(
        select(policies.c.id).where(
            policies.c.id == policy_id, policies.c.country_id == country
        )
    ).scalar_one_or_none()
    if found is None:
        msg = f"{country} has no policy {policy_id}"
        raise UnknownPolicyError(msg)
    return _read_reform(
        connection,
        owner=parameter_values.c.policy_id == policy_id,
        catalog_version_id=catalog_version_id,
        source="policy",
    )


def read_dynamic_reform(
    connection: Connection, *, dynamic_id: UUID, catalog_version_id: UUID
) -> PolicyReform:
    """Return the values the dynamic sets, by parameter name and period.

    The periods follow the same rule as a policy's. Raise
    UnknownDynamicError when there is no such dynamic, and
    MissingPolicyParametersError, naming the dynamic as its source, when
    ``catalog_version_id`` lacks a parameter it sets.
    """
    found = connection.execute(
        select(dynamics.c.id).where(dynamics.c.id == dynamic_id)
    ).scalar_one_or_none()
    if found is None:
        msg = f"there is no dynamic {dynamic_id}"
        raise UnknownDynamicError(msg)
    return _read_reform(
        connection,
        owner=parameter_values.c.dynamic_id == dynamic_id,
        catalog_version_id=catalog_version_id,
        source="dynamic",
    )


def combine_reforms(policy: PolicyReform, dynamic: PolicyReform) -> PolicyReform:
    """Return a simulation's policy values and its dynamic's, as one reform.

    Raise OverlappingReformError when both set a parameter, for any period.
    """
    overlapping = sorted(set(policy) & set(dynamic))
    if overlapping:
        raise OverlappingReformError(overlapping)
    return {**policy, **dynamic}


def _read_reform(
    connection: Connection,
    *,
    owner: ColumnElement[bool],
    catalog_version_id: UUID,
    source: ReformSource,
) -> PolicyReform:
    """Return the values ``owner`` selects, by parameter name and .py's periods.

    Raise MissingPolicyParametersError, naming ``source``, when
    ``catalog_version_id`` lacks a parameter they set.
    """
    values = connection.execute(
        select(
            parameters.c.name,
            parameter_values.c.value_json,
            parameter_values.c.start_date,
            parameter_values.c.end_date,
        )
        .join(parameters, parameters.c.id == parameter_values.c.parameter_id)
        .where(owner)
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
        raise MissingPolicyParametersError(missing, source=source)
    reform: PolicyReform = {}
    # The rows are ordered by parameter name, then start date, the order
    # .py takes them in; each value's periods are .py's.
    for position, row in enumerate(values):
        next_start_date = next(
            (
                later.start_date
                for later in values[position + 1 :]
                if later.name == row.name and later.start_date > row.start_date
            ),
            None,
        )
        period = _period(row.start_date, row.end_date, next_start_date)
        if period is None:
            # .py gives it a range ending before it starts (module docstring).
            continue
        reform.setdefault(row.name, {})[period] = row.value_json
    return reform


def _utc_day(moment: datetime) -> date:
    return moment.astimezone(UTC).date()
