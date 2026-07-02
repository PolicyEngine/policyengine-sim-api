"""Period-key parsing, validation, warnings, and normalization.

Ported from the v1 household API's ``policyengine_household_api/country.py``
(the period normalization / budget validation layer) so error and warning
messages match production byte-for-byte.

policyengine-core's ``period(value)`` is the canonical parser for situation
period keys. It returns a Period whose ``unit`` is one of "year" / "month" /
"day", and raises ValueError on garbage input. The household API only
distinguishes year vs. month, so we wrap that parser here.
"""

import copy
from dataclasses import dataclass

from policyengine_core.periods import period as parse_period


def _parsed_period(period_key: str):
    """Return a Period for a string key, or None if it doesn't parse.

    Wrapping policyengine-core's parser keeps the rest of this module free
    of regex and gives one place to handle malformed keys (e.g. ``"2026-15"``).
    """
    try:
        return parse_period(period_key)
    except (TypeError, ValueError):
        return None


def _is_year_key(period_key: str) -> bool:
    parsed = _parsed_period(period_key)
    return parsed is not None and parsed.unit == "year"


def _month_key_year(period_key: str) -> str | None:
    """Return the four-digit year if ``period_key`` parses as a month, else None."""
    parsed = _parsed_period(period_key)
    if parsed is None or parsed.unit != "month":
        return None
    return f"{parsed.start.year:04d}"


def _is_numeric(value) -> bool:
    """True for int/float numerics; rejects bool because ``bool`` ⊂ ``int`` in Python."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


# ---------------------------------------------------------------------------
# Household walk
# ---------------------------------------------------------------------------
#
# Validation, warning detection, and YEAR-key expansion all need the same
# household traversal: skip the top-level ``axes`` list, descend into each
# entity instance, and yield each ``(variable, period_map)`` pair. A single
# slot walker lets each pass focus on its rule instead of repeating the
# nested-loop boilerplate.


@dataclass(frozen=True)
class _VariableSlot:
    """A located period-map for one variable on one entity instance.

    ``period_map`` is the live dict, so callers that want to mutate it
    (the normalizer) and callers that only read (the validators and the
    warning detector) can share the same iterator.
    """

    entity_plural: str
    entity_id: str
    variable_name: str
    variable: object
    period_map: dict


def _walk_variable_slots(household: dict, system):
    """Yield a ``_VariableSlot`` for every (entity, variable) period map.

    Skips the top-level ``axes`` list, any non-dict value, and any
    variable name the system doesn't recognize (Pydantic rejects unknown
    names upstream; this is defense-in-depth so a stray key can't crash
    the walk).
    """
    for entity_plural, entities in (household or {}).items():
        if entity_plural == "axes" or not isinstance(entities, dict):
            continue
        for entity_id, entity_data in entities.items():
            if not isinstance(entity_data, dict):
                continue
            for variable_name, period_map in entity_data.items():
                if not isinstance(period_map, dict):
                    continue
                variable = system.variables.get(variable_name)
                if variable is None:
                    continue
                yield _VariableSlot(
                    entity_plural=entity_plural,
                    entity_id=entity_id,
                    variable_name=variable_name,
                    variable=variable,
                    period_map=period_map,
                )


# ---------------------------------------------------------------------------
# Period-key validation
# ---------------------------------------------------------------------------


def validate_period_keys(household: dict, system) -> None:
    """Reject any period key that doesn't parse as a year or month.

    policyengine-core's ``period()`` raises on malformed strings; without
    this check the engine would silently ignore the slot and the partner
    would see a confusing zero. Surfacing it as a 400 with the offending
    key gives them an explicit signal. Pydantic doesn't validate
    period-key strings today, so this is the single defensive checkpoint.
    """
    for slot in _walk_variable_slots(household, system):
        for period_key in slot.period_map:
            if _parsed_period(period_key) is None:
                raise ValueError(
                    f"Invalid period key `{period_key}` for "
                    f"`{slot.variable_name}` on "
                    f"`{slot.entity_plural}/{slot.entity_id}`. "
                    f'Expected a year (e.g. "2026") or a month '
                    f'(e.g. "2026-01").'
                )


# ---------------------------------------------------------------------------
# Period-shape warnings
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PartialMonthlyInputWarning:
    """Partial-month input on a MONTH-defined variable, paired with an
    annual output request on another MONTH-defined variable for the same
    year. The unset months read the engine's fallback (often 0, sometimes
    a formula-derived value) and silently inflate the annual sum.
    """

    variable: str
    entity_plural: str
    entity_id: str
    year: str
    months_set: tuple[int, ...]

    @property
    def message(self) -> str:
        sample = ", ".join(f"{self.year}-{m:02d}" for m in self.months_set[:3])
        if len(self.months_set) > 3:
            sample += ", ..."
        missing = 12 - len(self.months_set)
        return (
            f"`{self.variable}` on `{self.entity_plural}/{self.entity_id}` was keyed "
            f"for {len(self.months_set)} of 12 months in {self.year} ({sample}); "
            f"the remaining {missing} months will read the engine's "
            f"fallback value (often 0, sometimes a formula-derived value), "
            f"not the value you set. Because an annual output is requested "
            f"for {self.year}, those fallback values are summed into the annual "
            f"total and may not match what you intended. To get an accurate "
            f'annual figure, either send a yearly key (`{{"{self.year}": V}}`) '
            f"or set all 12 monthly keys."
        )


# Type alias for any warning the detector can emit.
PeriodWarning = PartialMonthlyInputWarning


def detect_period_warnings(household: dict, system) -> list[PeriodWarning]:
    """Return structured warnings for surprising request shapes.

    Currently surfaces one kind of warning:

    - ``PartialMonthlyInputWarning`` — partial monthly input on a
      MONTH-defined variable paired with an annual output request for
      the same year on any MONTH-defined variable. The unset months read
      the engine's fallback (often 0, sometimes a formula-derived value)
      and silently inflate the annual sum.

    The numeric output is unchanged — this is purely an additive
    partner-facing diagnostic.
    """
    warnings: list[PeriodWarning] = []
    annual_month_output_years: set[str] = set()
    monthly_inputs: dict[tuple[str, str, str, str], set[int]] = {}
    # `(variable, entity, year)` tuples whose period map already carries a
    # non-null year-key input. The normalizer fills the unset months from
    # the year-input remainder, so the partial-monthly hazard does not
    # apply — the unset months don't read the engine's fallback.
    years_with_year_input: set[tuple[str, str, str, str]] = set()

    for slot in _walk_variable_slots(household, system):
        is_month_var = slot.variable.definition_period == "month"
        for period_key, value in slot.period_map.items():
            if value is None:
                # Annual nulls on MONTH vars arm the missing-month
                # hazard. YEAR-defined vars don't have months.
                if is_month_var and _is_year_key(period_key):
                    annual_month_output_years.add(period_key)
                continue
            if not is_month_var:
                continue
            if _is_year_key(period_key):
                years_with_year_input.add(
                    (
                        slot.variable_name,
                        slot.entity_plural,
                        slot.entity_id,
                        period_key,
                    )
                )
                continue
            year = _month_key_year(period_key)
            if year is None:
                continue
            parsed = _parsed_period(period_key)
            key = (
                slot.variable_name,
                slot.entity_plural,
                slot.entity_id,
                year,
            )
            monthly_inputs.setdefault(key, set()).add(parsed.start.month)

    for (
        variable_name,
        entity_plural,
        entity_id,
        year,
    ), months in monthly_inputs.items():
        if year not in annual_month_output_years:
            continue
        if len(months) >= 12:
            continue
        # If the same period map also has a non-null year input for this
        # year, the normalizer fills the unset months from the remainder
        # — there's no missing-month hazard to warn about.
        if (
            variable_name,
            entity_plural,
            entity_id,
            year,
        ) in years_with_year_input:
            continue
        warnings.append(
            PartialMonthlyInputWarning(
                variable=variable_name,
                entity_plural=entity_plural,
                entity_id=entity_id,
                year=year,
                months_set=tuple(sorted(months)),
            )
        )
    return warnings


# ---------------------------------------------------------------------------
# Period-key normalization
# ---------------------------------------------------------------------------
#
# When a numeric MONTH-defined variable receives both a year-key and same-
# year monthly-key inputs, the API mirrors the hosted v1 API and OpenFisca's
# ``set_input_divide_by_period``: treat the year value as the annual total,
# subtract any explicit monthly inputs, and split the remainder evenly
# across the unset months (raw float, no rounding). Boolean / string / enum
# year values broadcast to the unset months unchanged. If the explicit
# monthlies sum to more than the annual total, ``validate_period_budgets``
# rejects the request with a 400 (matching the ``ValueError("Inconsistent
# input...")`` that OpenFisca raises in the same situation).


def _is_numeric_value_type(variable) -> bool:
    """True iff the variable's value_type is int or float (excluding bool)."""
    vt = variable.value_type
    return vt in (int, float)


def validate_period_budgets(household: dict, system) -> None:
    """Reject requests where all 12 monthly inputs disagree with the annual.

    For numeric MONTH-defined variables, partners may send both a year key
    (the annual total) and one or more monthly overrides. policyengine-core's
    ``set_input_divide_by_period`` raises ``ValueError("Inconsistent input")``
    in exactly one situation: every month of the year is explicit AND the
    sum of those monthlies doesn't match the annual total. Partial-month
    overrides (any number from 0 to 11 explicit months) are silently
    accepted and the remainder is distributed across the unset months,
    even when the remainder is negative. We mirror that exact rule so
    partner integrations match v1 byte-for-byte.
    """
    for slot in _walk_variable_slots(household, system):
        if slot.variable.definition_period != "month":
            continue
        if not _is_numeric_value_type(slot.variable):
            continue
        _check_year_budget(
            period_map=slot.period_map,
            variable_name=slot.variable_name,
            entity_plural=slot.entity_plural,
            entity_id=slot.entity_id,
        )


def _check_year_budget(
    period_map: dict,
    variable_name: str,
    entity_plural: str,
    entity_id: str,
) -> None:
    for period_key, value in list(period_map.items()):
        if not _is_year_key(period_key):
            continue
        if not _is_numeric(value):
            continue
        year = period_key
        explicit_months: dict[int, float] = {}
        for inner_key, inner_value in period_map.items():
            if _month_key_year(inner_key) != year:
                continue
            if not _is_numeric(inner_value):
                continue
            month = _parsed_period(inner_key).start.month
            explicit_months[month] = float(inner_value)
        # v1 raises only when every month is explicit and the sum
        # disagrees with the annual; partial monthlies are silently
        # distributed (even if the remainder is negative).
        if len(explicit_months) < 12:
            continue
        explicit_sum = sum(explicit_months.values())
        if explicit_sum != float(value):
            raise ValueError(
                f"Inconsistent input: monthly values for `{variable_name}` on "
                f"`{entity_plural}/{entity_id}` in {year} sum to "
                f"{explicit_sum}, which doesn't match the annual total {value}."
            )


def normalize_period_keys(household: dict, system) -> dict:
    """Return a deep-copied household with YEAR-keyed inputs expanded to months.

    policyengine-core's ``Simulation(situation=...)`` silently drops a year-
    period assignment on a MONTH-defined variable (see issue #1489). The
    hosted v1 API distributes the annual value across the year so partners
    get a sensible answer. To match that behavior, this normalizer walks the
    situation, looks up each variable's ``definition_period``, and rewrites
    YEAR-keyed values for MONTH-defined variables before the engine sees them.

    Behavior per value type:

    - **numeric**: the year value is treated as the annual total. Any
      explicit monthly values for the same year are preserved (they
      override that month) and the remainder ``V − sum(explicit)`` is
      split evenly across the unset months as a raw float — matches
      v1's ``163.63637``-style emission. Rounding here would introduce
      drift the engine sums back differently.
    - **boolean / string / enum**: the year value is broadcast unchanged
      to the unset months. Explicit monthly values still override.
    - **null (output request)**: the YEAR key is left alone so the engine
      returns the annual sum across the 12 monthly values.

    The original household is never mutated, so the response can echo the
    partner's keys verbatim.
    """
    normalized = copy.deepcopy(household)
    for slot in _walk_variable_slots(normalized, system):
        if slot.variable.definition_period != "month":
            continue
        _expand_year_keys_in_place(slot.period_map, slot.variable)
    return normalized


def _expand_year_keys_in_place(period_map: dict, variable) -> None:
    is_numeric = _is_numeric_value_type(variable)
    for period_key in list(period_map.keys()):
        if not _is_year_key(period_key):
            continue
        value = period_map[period_key]
        if value is None:
            # Output request — keep the YEAR key so the engine sums the months.
            continue
        year = period_key
        if is_numeric and _is_numeric(value):
            _distribute_numeric_year_value(period_map, year, float(value))
        else:
            _broadcast_year_value(period_map, year, value)


def _distribute_numeric_year_value(
    period_map: dict, year: str, annual_value: float
) -> None:
    """Distribute an annual numeric V across the 12 months of ``year``.

    Explicit monthly values for the same year are preserved (the partner
    is overriding that month). The remainder ``V − sum(explicit)`` is
    split evenly across the unset months as a raw float, matching the
    hosted v1 API and OpenFisca's ``set_input_divide_by_period`` —
    including the case where the remainder is negative (sum of explicit
    months > annual). ``validate_period_budgets`` only rejects the
    fully-explicit-12-months case where sum != annual; partial monthlies
    are silently distributed even if that pushes the unset months
    negative, exactly like v1.
    """
    explicit_months: dict[int, float] = {}
    for inner_key, inner_value in period_map.items():
        if _month_key_year(inner_key) != year:
            continue
        if not _is_numeric(inner_value):
            continue
        month = _parsed_period(inner_key).start.month
        explicit_months[month] = float(inner_value)

    unset_count = 12 - len(explicit_months)
    if unset_count <= 0:
        # All 12 months are explicit. validate_period_budgets has already
        # confirmed they sum to == annual_value, so just remove the YEAR key.
        del period_map[year]
        return

    remainder = annual_value - sum(explicit_months.values())
    per_unset = remainder / unset_count
    del period_map[year]
    for month in range(1, 13):
        month_key = f"{year}-{month:02d}"
        if month in explicit_months:
            continue
        # Overwrite missing slots and None (output-request) slots; explicit
        # non-null monthly inputs are already in `explicit_months` so this
        # branch only fires for unset / null slots.
        if period_map.get(month_key) is None:
            period_map[month_key] = per_unset


def _broadcast_year_value(period_map: dict, year: str, value) -> None:
    """Broadcast a non-numeric annual value (bool/str/enum) to every month.

    Explicit monthly values already in the period map win — partners can
    set a year-wide value and override one month explicitly.
    """
    del period_map[year]
    for month in range(1, 13):
        month_key = f"{year}-{month:02d}"
        if period_map.get(month_key) is None:
            period_map[month_key] = value
