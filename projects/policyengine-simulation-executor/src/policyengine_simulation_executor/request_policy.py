"""Read a simulation request's year, and the policy its periods describe."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from types import ModuleType

    from policyengine.core import Policy

DEFAULT_YEAR = 2026

# A policy period key is one date or a ``start.stop`` pair of dates.
_DATE_RANGE_PART_COUNT = 2


def parse_year(params: dict[str, Any]) -> int:
    """Return the request's simulation year, or the default year without one."""
    value = params.get("time_period") or params.get("year") or DEFAULT_YEAR
    return int(value)


def _parse_policy_period(period_key: object) -> tuple[datetime, datetime | None]:
    """Parse effective dates or inclusive ``start.stop`` policy periods."""
    text = str(period_key)
    try:
        return _parse_policy_period_dates(text)
    except ValueError:
        msg = f"Invalid policy period key: {text!r}"
        raise ValueError(msg) from None


def _parse_policy_period_dates(text: str) -> tuple[datetime, datetime | None]:
    """Parse a date or ``start.stop`` range; raise a bare ``ValueError`` if invalid."""
    parts = text.split(".")
    if len(parts) not in {1, 2}:
        raise ValueError
    # The wrapper has always parsed dates with strptime, which also takes
    # unpadded months and days ("2027-4-6"); accept what it accepted.
    dates = [datetime.strptime(part, "%Y-%m-%d") for part in parts]  # noqa: DTZ007  # naive like policyengine.py's own period parsing; its ParameterValue dates are naive
    start = dates[0]
    stop = dates[1] if len(dates) == _DATE_RANGE_PART_COUNT else None
    if stop is not None and stop < start:
        raise ValueError
    return start, stop


def _normalise_period_key(period_key: object) -> str:
    """Validate policy period keys without discarding their stop dates."""
    text = str(period_key)
    _parse_policy_period(text)
    return text


def normalise_policy(policy: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return the request's policy with every period key normalised, or None."""
    if not policy:
        return None

    normalised: dict[str, Any] = {}
    for parameter, value in policy.items():
        if isinstance(value, dict):
            normalised[parameter] = {
                _normalise_period_key(period): period_value
                for period, period_value in value.items()
            }
        else:
            normalised[parameter] = value
    return normalised


def compile_interval_policy(
    policy: dict[str, Any] | None, *, country_module: ModuleType, year: int
) -> dict[str, Any] | Policy | None:
    """Use public wrapper values for intervals its dict compiler cannot parse."""
    if not policy or not any(
        isinstance(value, dict) and any("." in str(key) for key in value)
        for value in policy.values()
    ):
        return policy

    from policyengine.core import (  # noqa: PLC0415  # this module keeps policyengine out of its module-level imports (see its docstring)
        ParameterValue,
        Policy,
    )
    from policyengine.core.parameter import (  # noqa: PLC0415  # this module keeps policyengine out of its module-level imports (see its docstring)
        Parameter,
    )
    from policyengine.tax_benefit_models.common.reform import (
        compile_reform,
    )

    compiled = compile_reform(policy, year=year, model_version=country_module.model)
    if compiled is None:
        # compile_reform returns None only for an empty policy, returned above.
        return policy
    parameter_values = []
    for path, periods in compiled.items():
        dated_values = [
            (*_parse_policy_period(key), value) for key, value in periods.items()
        ]
        for start, stop, value in sorted(dated_values, key=lambda item: item[0]):
            # Match the wrapper's scalar type selection and effective dates.
            data_type = type(value) if isinstance(value, (int, float, bool)) else float
            parameter_values.append(
                ParameterValue(
                    parameter=Parameter(
                        name=path,
                        tax_benefit_model_version=country_module.model,
                        data_type=data_type,
                    ),
                    start_date=start,
                    end_date=stop,
                    value=value,
                )
            )
    return Policy(name="Reform", parameter_values=parameter_values)


def wrapper_policy(
    policy: dict[str, Any] | None, *, country_module: ModuleType, year: int
) -> dict[str, Any] | Policy | None:
    """Convert a request's policy into the form the wrapper must receive.

    Normalises the period keys, then compiles a policy with interval keys
    into wrapper parameter values (see ``_compile_interval_policy``). Every
    path that hands a request policy to the wrapper goes through this.
    """
    return compile_interval_policy(
        normalise_policy(policy), country_module=country_module, year=year
    )
