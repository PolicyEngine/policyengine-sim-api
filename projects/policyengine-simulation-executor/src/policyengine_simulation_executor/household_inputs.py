"""Household input preparation, matching the Public API's in-process calculation.

These functions reproduce, for the v2 household functions, how the Public API
prepares a household before calculating it: which missing variables are added,
which deprecated inputs are dropped (and the warning that says so), which
names are reported as unrecognized, and which values are requested. Keeping
the wording and order identical lets a v2 result be compared with the Public
API's result field by field. Missing variables are added for the request's
year, where the Public API infers one.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from policyengine_simulation_contract.household_execution import HouseholdInputError

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from policyengine_core.taxbenefitsystems import TaxBenefitSystem
    from policyengine_core.variables import Variable
    from policyengine_simulation_contract.household_execution import InputErrorType

# Removed or renamed model variables that callers may still send. The value is
# the migration hint shown in the warning. Matches the Public API's registry.
DEPRECATED_VARIABLES: dict[str, str] = {
    "medical_out_of_pocket_expenses": (
        "Removed in policyengine-us 1.673.0. Migrate non-premium spending "
        "to `other_medical_expenses` and premium spending to "
        "`health_insurance_premiums`."
    ),
    "uc_has_limited_capability_for_work": (
        "Removed in policyengine-uk 2.129.0. Set "
        "`uc_limited_capability_for_work` instead."
    ),
}
# Variables with these definition periods are added when missing.
_ADDED_DEFINITION_PERIODS = frozenset({"year", "month", "eternity"})
_AXES_KEY = "axes"

# One value to calculate: entity plural, entity name, variable and period.
RequestedValue = tuple[str, str, str, str]


@dataclass(frozen=True)
class HouseholdVariable:
    """One country-model variable, as household preparation needs it.

    ``default_value`` is the value the Public API's metadata reports: the
    enum member name for an enum variable, the default for a boolean or
    numeric variable, and None otherwise.
    """

    name: str
    entity: str
    definition_period: str
    is_input: bool
    default_value: bool | int | float | str | None


@dataclass(frozen=True)
class HouseholdEntity:
    """One country-model entity: its key, plural and the plurals of its roles."""

    key: str
    plural: str
    role_plurals: frozenset[str]


@dataclass(frozen=True)
class HouseholdModelMetadata:
    """The variables, entities and policy parameters of one country model."""

    variables: Mapping[str, HouseholdVariable]
    entities: Mapping[str, HouseholdEntity]
    parameters: frozenset[str]

    @classmethod
    def from_tax_benefit_system(
        cls, system: TaxBenefitSystem
    ) -> HouseholdModelMetadata:
        """Read the metadata the Public API builds from a tax-benefit system.

        Variables keep the system's order. Parameters are every descendant
        whose name starts with ``gov``, as the Public API's metadata lists.
        """
        entities = {
            entity.key: HouseholdEntity(
                key=entity.key,
                plural=entity.plural,
                role_plurals=frozenset(
                    role.plural
                    for role in getattr(entity, "roles", ())
                    if role.plural is not None
                ),
            )
            for entity in system.entities
        }
        variables = {
            name: HouseholdVariable(
                name=name,
                entity=variable.entity.key,
                definition_period=variable.definition_period,
                is_input=variable.is_input_variable(),
                default_value=_metadata_default_value(variable),
            )
            for name, variable in system.variables.items()
        }
        root = system.parameters
        parameters = frozenset(
            parameter.name
            for parameter in (root.get_descendants() if root is not None else ())
            if parameter.name[:3] == "gov"
        )
        return cls(variables=variables, entities=entities, parameters=parameters)

    def entity_by_plural(self) -> dict[str, HouseholdEntity]:
        """Return the entities keyed by their plural."""
        return {entity.plural: entity for entity in self.entities.values()}


def _metadata_default_value(variable: Variable) -> bool | int | float | str | None:
    default_value: object = variable.default_value
    if variable.value_type.__name__ == "Enum":
        return str(getattr(default_value, "name", default_value))
    if isinstance(default_value, (int, float, bool)):
        return default_value
    return None


def with_request_year(situation: dict[str, Any], *, year: str) -> None:
    """Give ``year`` to every axis and value the household gives without a period.

    A variable given as one value, not by period, becomes that value for
    ``year``, and an axis without a ``period`` varies ``year``. The country
    model would otherwise put both in its own default year (2024 for the US
    model, 2025 for the UK model), as the Public API leaves it to, whatever
    year the household is calculated for. Membership lists are left alone.
    The situation is changed in place.
    """
    for entity_plural, entity_group in situation.items():
        if entity_plural == _AXES_KEY:
            _give_axes_the_year(entity_group, year)
        elif isinstance(entity_group, dict):
            for variables in entity_group.values():
                if isinstance(variables, dict):
                    _date_single_values(variables, year)


def _date_single_values(variables: dict[str, Any], year: str) -> None:
    for variable_name, value in variables.items():
        if value is None or isinstance(value, (bool, int, float, str)):
            variables[variable_name] = {year: value}


def _give_axes_the_year(axes: object, year: str) -> None:
    if not isinstance(axes, list):
        return
    for group in axes:
        for axis in group:
            if isinstance(axis, dict) and axis.get("period") is None:
                axis["period"] = year


def add_missing_variables(
    situation: dict[str, Any],
    metadata: HouseholdModelMetadata,
    *,
    year: str,
) -> None:
    """Add every yearly, monthly and eternal variable each entity lacks, in place.

    Input variables get their default value and calculated variables get
    None, which requests them, for ``year``, the request's calculation year.
    The Public API's ``/calculate-full`` route adds the same variables, for
    the first age period of a person named ``you`` or else the current year.
    """
    for variable in metadata.variables.values():
        if variable.definition_period not in _ADDED_DEFINITION_PERIODS:
            continue
        entity_plural = metadata.entities[variable.entity].plural
        for entity in situation.get(entity_plural, {}).values():
            if variable.name not in entity:
                entity[variable.name] = {
                    year: variable.default_value if variable.is_input else None
                }


@dataclass(frozen=True)
class DeprecatedVariableWarning:
    """A removed or renamed variable was supplied and dropped before calculation."""

    variable: str
    entity_plural: str
    entity_id: str
    hint: str

    @property
    def message(self) -> str:
        """Return the warning naming the ignored input, where it was and the hint."""
        location = f"`{self.entity_plural}/{self.entity_id}`"
        if self.entity_plural == _AXES_KEY:
            location = f"`axes[{self.entity_id}].name`"
        return (
            f"Input `{self.variable}` on {location} is deprecated and was "
            f"ignored for this calculation. {self.hint}"
        )


@dataclass(frozen=True)
class DeprecatedInputsResult:
    """A household copy without deprecated inputs, and one warning per input."""

    household: dict[str, Any]
    warnings: tuple[DeprecatedVariableWarning, ...]


def drop_deprecated_inputs(situation: Mapping[str, Any]) -> DeprecatedInputsResult:
    """Return a copy of ``situation`` without deprecated inputs, with warnings.

    Each deprecated variable on an entity, and each axis that varies one, is
    removed with one warning; an axis group left empty is removed, and
    ``axes`` is removed when no group is left. ``situation`` is not changed.
    """
    warnings: list[DeprecatedVariableWarning] = []
    household = copy.deepcopy(dict(situation))
    for entity_plural, entity_group in household.items():
        if entity_plural == _AXES_KEY or not isinstance(entity_group, dict):
            continue
        for entity_id, variables in entity_group.items():
            if isinstance(variables, dict):
                warnings.extend(
                    _drop_deprecated_variables(entity_plural, entity_id, variables)
                )
    _drop_deprecated_axes(household, warnings)
    return DeprecatedInputsResult(household=household, warnings=tuple(warnings))


def _drop_deprecated_variables(
    entity_plural: str, entity_id: str, variables: dict[str, Any]
) -> list[DeprecatedVariableWarning]:
    warnings: list[DeprecatedVariableWarning] = []
    for variable_name in list(variables):
        hint = DEPRECATED_VARIABLES.get(variable_name)
        if hint is None:
            continue
        warnings.append(
            DeprecatedVariableWarning(
                variable=variable_name,
                entity_plural=entity_plural,
                entity_id=entity_id,
                hint=hint,
            )
        )
        del variables[variable_name]
    return warnings


def _drop_deprecated_axes(
    household: dict[str, Any], warnings: list[DeprecatedVariableWarning]
) -> None:
    axes = household.get(_AXES_KEY)
    if not isinstance(axes, list):
        return
    changed = False
    retained_groups = []
    for group_index, group in enumerate(axes):
        retained_axes = []
        for axis_index, axis in enumerate(group):
            warning = _deprecated_axis_warning(axis, f"{group_index}][{axis_index}")
            if warning is None:
                retained_axes.append(axis)
                continue
            warnings.append(warning)
            changed = True
        if retained_axes:
            retained_groups.append(retained_axes)
    if not changed:
        return
    if retained_groups:
        household[_AXES_KEY] = retained_groups
    else:
        del household[_AXES_KEY]


def _deprecated_axis_warning(
    axis: object, location: str
) -> DeprecatedVariableWarning | None:
    if not isinstance(axis, dict):
        return None
    variable_name = str(axis.get("name"))
    hint = DEPRECATED_VARIABLES.get(variable_name)
    if hint is None:
        return None
    return DeprecatedVariableWarning(
        variable=variable_name, entity_plural=_AXES_KEY, entity_id=location, hint=hint
    )


def _input_error_message(
    input_type: InputErrorType,
    name: str,
    path: str,
    entity_plurals: tuple[str, str] | None,
) -> str:
    if input_type == "household_entity":
        return f"Unrecognized household entity group `{name}` at `{path}`."
    if input_type == "household_variable_wrong_entity" and entity_plurals:
        expected, actual = entity_plurals
        return (
            f"Household variable `{name}` belongs on `{expected}`, "
            f"not `{actual}`, at `{path}`."
        )
    if input_type == "household_axis_variable":
        return f"Unrecognized household axis variable `{name}` at `{path}`."
    if input_type == "policy_parameter":
        return f"Unrecognized policy parameter `{name}` at `{path}`."
    return f"Unrecognized household variable `{name}` at `{path}`."


def _input_error(
    input_type: InputErrorType,
    name: str,
    path: str,
    *,
    entity_plurals: tuple[str, str] | None = None,
) -> HouseholdInputError:
    """Return one unrecognized input, worded as the Public API words it.

    ``entity_plurals`` is the (expected, actual) pair for a variable placed on
    the wrong entity.
    """
    expected, actual = entity_plurals or (None, None)
    return HouseholdInputError(
        type=input_type,
        name=name,
        path=path,
        message=_input_error_message(input_type, name, path, entity_plurals),
        expected_entity_plural=expected,
        actual_entity_plural=actual,
    )


def find_unrecognized_household_inputs(
    situation: Mapping[str, Any],
    metadata: HouseholdModelMetadata,
) -> list[HouseholdInputError]:
    """Find unknown entity groups, variables and axis variables, in document order.

    Also reports a known variable placed on the wrong entity. Role membership
    keys (such as ``members``) are not variables and are skipped.
    """
    errors: list[HouseholdInputError] = []
    entity_by_plural = metadata.entity_by_plural()
    for entity_plural, entity_group in situation.items():
        if entity_plural == _AXES_KEY:
            errors.extend(_find_unrecognized_axes(entity_group, metadata))
            continue
        entity = entity_by_plural.get(entity_plural)
        if entity is None:
            errors.append(
                _input_error(
                    "household_entity", entity_plural, f"household.{entity_plural}"
                )
            )
            continue
        if not isinstance(entity_group, dict):
            continue
        for entity_id, entity_inputs in entity_group.items():
            if isinstance(entity_inputs, dict):
                errors.extend(
                    _find_unrecognized_entity_inputs(
                        entity, entity_id, entity_inputs, metadata
                    )
                )
    return errors


def _find_unrecognized_entity_inputs(
    entity: HouseholdEntity,
    entity_id: str,
    entity_inputs: Mapping[str, Any],
    metadata: HouseholdModelMetadata,
) -> list[HouseholdInputError]:
    errors: list[HouseholdInputError] = []
    for variable_name in entity_inputs:
        if variable_name in entity.role_plurals:
            continue
        variable = metadata.variables.get(variable_name)
        path = f"household.{entity.plural}.{entity_id}.{variable_name}"
        if variable is None:
            errors.append(_input_error("household_variable", variable_name, path))
            continue
        expected_entity = metadata.entities.get(variable.entity)
        if expected_entity is not None and expected_entity.plural != entity.plural:
            errors.append(
                _input_error(
                    "household_variable_wrong_entity",
                    variable_name,
                    path,
                    entity_plurals=(expected_entity.plural, entity.plural),
                )
            )
    return errors


def _find_unrecognized_axes(
    axes: object, metadata: HouseholdModelMetadata
) -> list[HouseholdInputError]:
    if not isinstance(axes, list):
        return []
    errors: list[HouseholdInputError] = []
    for group_index, group in enumerate(axes):
        for axis_index, axis in enumerate(group):
            if not isinstance(axis, dict) or "name" not in axis:
                continue
            variable_name = str(axis["name"])
            if variable_name in metadata.variables:
                continue
            errors.append(
                _input_error(
                    "household_axis_variable",
                    variable_name,
                    f"household.axes[{group_index}][{axis_index}].name",
                )
            )
    return errors


def find_unrecognized_policy_inputs(
    parameters: Iterable[str],
    metadata: HouseholdModelMetadata,
) -> list[HouseholdInputError]:
    """Find policy parameter paths the country model does not have, in order."""
    return [
        _input_error("policy_parameter", name, f"policy.{name}")
        for name in parameters
        if name not in metadata.parameters
    ]


def unrecognized_inputs_message(errors: Iterable[HouseholdInputError]) -> str:
    """Join the errors' messages as the Public API's error response does."""
    return "Unrecognized calculate input(s): " + "; ".join(
        error.message for error in errors
    )


def requested_computations(
    situation: Mapping[str, Any],
    *,
    include_provided_values: bool,
    variable_names: set[str] | frozenset[str] | None = None,
) -> list[RequestedValue]:
    """List the (entity plural, entity name, variable, period) values to calculate.

    A value is requested when it is None or, with ``include_provided_values``,
    whatever it is. ``variable_names`` keeps only those variables. Values are
    listed in document order, as the Public API lists them.
    """
    computations: list[RequestedValue] = []
    for entity_plural, entities in situation.items():
        if not isinstance(entities, dict):
            continue
        for entity_id, variables in entities.items():
            if isinstance(variables, dict):
                computations.extend(
                    _requested_entity_values(
                        (entity_plural, entity_id),
                        variables,
                        include_provided_values=include_provided_values,
                        variable_names=variable_names,
                    )
                )
    return computations


def _requested_entity_values(
    entity: tuple[str, str],
    variables: Mapping[str, Any],
    *,
    include_provided_values: bool,
    variable_names: set[str] | frozenset[str] | None,
) -> list[RequestedValue]:
    entity_plural, entity_id = entity
    return [
        (entity_plural, entity_id, variable_name, str(period))
        for variable_name, periods in variables.items()
        if isinstance(periods, dict)
        and (variable_names is None or variable_name in variable_names)
        for period, value in periods.items()
        if include_provided_values or value is None
    ]
