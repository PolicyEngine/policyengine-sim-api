"""Read a stored policy, or dynamic, as the reform values a calculation applies.

API v1 stores a policy's parameter values against the parameters of
the catalog version it was saved on, each from a start date until an end
date, or with no end date indefinitely. A calculation on any catalog version
applies them by parameter name, as ``{parameter name: {period: value}}``, the
shape of API v1's policy JSON that the household functions' contract
and the economy requests take. A dynamic's values are stored and read the same
way, and a dynamic, like a policy, belongs to one country. A simulation,
economy or household, applies its policy's and its dynamic's values together
(``read_simulation_reform``), and is refused when both set the same parameter
(``combine_reforms``).

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

from dataclasses import dataclass
from datetime import UTC, date, timedelta
from typing import TYPE_CHECKING, Final, Literal

from sqlalchemy import select

from policyengine_simulation_persistence.input_tables import (
    Dynamic,
    Parameter,
    ParameterValue,
    Policy,
)
from policyengine_simulation_persistence.tables import LATEST_YEAR

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime
    from uuid import UUID

    from sqlalchemy import ColumnElement
    from sqlalchemy.orm import Session

# The last day a value without an end date is applied.
OPEN_ENDED_STOP: Final = date(LATEST_YEAR, 12, 31)

# A policy's reform values: each parameter's values by period.
type PolicyReform = dict[str, dict[str, object]]
# What stores the values a calculation applies.
type ReformSource = Literal["policy", "dynamic"]


class UnknownPolicyError(LookupError):
    """API v1 has no such policy in the country."""


class UnknownDynamicError(LookupError):
    """API v1 has no such dynamic in the country."""


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
    session: Session,
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
    found = session.execute(
        select(Policy.id).where(Policy.id == policy_id, Policy.country_id == country)
    ).scalar_one_or_none()
    if found is None:
        msg = f"{country} has no policy {policy_id}"
        raise UnknownPolicyError(msg)
    return _read_reform(
        session,
        owner=ParameterValue.policy_id == policy_id,
        catalog_version_id=catalog_version_id,
        source="policy",
    )


def read_dynamic_reform(
    session: Session,
    *,
    country: str,
    dynamic_id: UUID,
    catalog_version_id: UUID,
) -> PolicyReform:
    """Return the values ``country``'s dynamic sets, by parameter name and period.

    The periods follow the same rule as a policy's. Raise
    UnknownDynamicError when the country has no such dynamic, a dynamic
    stored for another country included, and MissingPolicyParametersError,
    naming the dynamic as its source, when ``catalog_version_id`` lacks a
    parameter it sets.
    """
    found = session.execute(
        select(Dynamic.id).where(
            Dynamic.id == dynamic_id, Dynamic.country_id == country
        )
    ).scalar_one_or_none()
    if found is None:
        msg = f"{country} has no dynamic {dynamic_id}"
        raise UnknownDynamicError(msg)
    return _read_reform(
        session,
        owner=ParameterValue.dynamic_id == dynamic_id,
        catalog_version_id=catalog_version_id,
        source="dynamic",
    )


@dataclass(frozen=True)
class SimulationReform:
    """The values a simulation applies: its policy's and its dynamic's together.

    ``values`` holds both by parameter name and period, empty for current
    law with no dynamic. ``dynamic_parameters`` names the parameters the
    dynamic sets, none of which the policy sets.
    """

    values: PolicyReform
    dynamic_parameters: frozenset[str]


def read_simulation_reform(
    session: Session,
    *,
    country: str,
    policy_id: UUID | None,
    dynamic_id: UUID | None,
    catalog_version_id: UUID,
) -> SimulationReform:
    """Return the values ``country``'s simulation applies: its policy's and dynamic's.

    None for either reads nothing for it. Raise UnknownPolicyError or
    UnknownDynamicError for one the country doesn't have,
    MissingPolicyParametersError for one setting a parameter
    ``catalog_version_id`` lacks, and OverlappingReformError when both set a
    parameter.
    """
    policy = (
        {}
        if policy_id is None
        else read_policy_reform(
            session,
            country=country,
            policy_id=policy_id,
            catalog_version_id=catalog_version_id,
        )
    )
    dynamic = (
        {}
        if dynamic_id is None
        else read_dynamic_reform(
            session,
            country=country,
            dynamic_id=dynamic_id,
            catalog_version_id=catalog_version_id,
        )
    )
    return SimulationReform(
        values=combine_reforms(policy, dynamic), dynamic_parameters=frozenset(dynamic)
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
    session: Session,
    *,
    owner: ColumnElement[bool],
    catalog_version_id: UUID,
    source: ReformSource,
) -> PolicyReform:
    """Return the values ``owner`` selects, by parameter name and .py's periods.

    Raise MissingPolicyParametersError, naming ``source``, when
    ``catalog_version_id`` lacks a parameter they set.
    """
    values = session.execute(
        select(
            Parameter.name,
            ParameterValue.value_json,
            ParameterValue.start_date,
            ParameterValue.end_date,
        )
        .select_from(ParameterValue)
        .join(Parameter, Parameter.id == ParameterValue.parameter_id)
        .where(owner)
        .order_by(Parameter.name, ParameterValue.start_date)
    ).all()
    names = sorted({row.name for row in values})
    available = set(
        session.execute(
            select(Parameter.name).where(
                Parameter.tax_benefit_model_version_id == catalog_version_id,
                Parameter.name.in_(names),
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
