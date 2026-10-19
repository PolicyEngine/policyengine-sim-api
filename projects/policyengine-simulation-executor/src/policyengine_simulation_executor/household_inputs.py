"""Household input preparation, matching the Public API's in-process calculation.

These functions reproduce, for the v2 household functions, how the Public API
prepares a household before calculating it: which period missing variables
are added for, which deprecated inputs are dropped (and the warnings that
says so), which names are reported as unrecognized, and which values are
requested. Keeping the wording and order identical lets a v2 result be
compared with the Public API's result field by field.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from policyengine_simulation_contract.household_execution import HouseholdInputError

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
    from datetime import date

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
    def from_tax_benefit_system(cls, system: Any) -> HouseholdModelMetadata:  # noqa: ANN401  # a country model's TaxBenefitSystem, read by attribute only
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
        parameters = frozenset(
            parameter.name
            for parameter in system.parameters.get_descendants()
            if parameter.name[:3] == "gov"
        )
        return cls(variables=variables, entities=entities, parameters=parameters)

    def entity_by_plural(self) -> dict[str, HouseholdEntity]:
        """Return the entities keyed by their plural."""
        return {entity.plural: entity for entity in self.entities.values()}


def _metadata_default_value(variable: Any) -> bool | int | float | str | None:  # noqa: ANN401  # a country model's Variable, read by attribute only
    if variable.value_type.__name__ == "Enum":
        return variable.default_value.name
    if isinstance(variable.default_value, (int, float, bool)):
        return variable.default_value
    return None


def household_year(situation: Mapping[str, Any], *, today: date) -> str:
    """Return the period missing yearly variables are added for.

    As in the Public API, this is the first period of the age entered for the
    person named ``you``, or else the current calendar year. An age entered
    without periods counts as no age.
    """
    you = situation.get("people", {}).get("you", {})
    ages = you.get("age") if isinstance(you, dict) else None
    if isinstance(ages, dict) and ages:
        return str(next(iter(ages)))
    return str(today.year)


def add_missing_variables(
    situation: dict[str, Any],
    metadata: HouseholdModelMetadata,
    *,
    today: date,
) -> None:
    """Add every yearly, monthly and eternal variable each entity lacks, in place.

    Input variables get their default value and calculated variables get
    None, which requests them, for the household's year. This is what the
    Public API's ``/calculate-full`` route adds.
    """
    year = household_year(situation, today=today)
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
        if self.entity_plural == "axes":
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

    Each deprecated variable on an entity, and each axis that varies one,
    is removed with one warning; an axis group left empty is removed, and
    ``axes`` is removed when no group is left. ``situation`` is not changed.
    """
    warnings: list[DeprecatedVariableWarning] = []
    household = copy.deepcopy(dict(situation))
    for entity_plural, entity_group in household.items():
        if entity_plural == "axes" or not isinstance(entity_group, dict):
            continue
        for entity_id, variables in entity_group.items():
            if not isinstance(variables, dict):
                continue
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
    _drop_deprecated_axes(household, warnings)
    return DeprecatedInputsResult(household=household, warnings=tuple(warnings))


def _drop_deprecated_axes(
    household: dict[str, Any], warnings: list[DeprecatedVariableWarning]
) -> None:
    axes = household.get("axes")
    if not isinstance(axes, list):
        return
    changed = False
    retained_groups = []
    for group_index, group in enumerate(axes):
        retained_axes = []
        for axis_index, axis in enumerate(group):
            location = f"{group_index}][{axis_index}"
            if _is_deprecated_axis(axis, location, warnings):
                changed = True
                continue
            retained_axes.append(axis)
        if retained_axes:
            retained_groups.append(retained_axes)
    if not changed:
        return
    if retained_groups:
        household["axes"] = retained_groups
    else:
        del household["axes"]


def _is_deprecated_axis(
    axis: object, location: str, warnings: list[DeprecatedVariableWarning]
) -> bool:
    if not isinstance(axis, dict):
        return False
    variable_name = axis.get("name")
    hint = DEPRECATED_VARIABLES.get(str(variable_name))
    if hint is None:
        return False
    warnings.append(
        DeprecatedVariableWarning(
            variable=str(variable_name),
            entity_plural="axes",
            entity_id=location,
            hint=hint,
        )
    )
    return True


def _input_error(
    input_type: InputErrorType,
    name: str,
    path: str,
    *,
    expected_entity_plural: str | None = None,
    actual_entity_plural: str | None = None,
) -> HouseholdInputError:
    if input_type == "household_entity":
        message = f"Unrecognized household entity group `{name}` at `{path}`."
    elif input_type == "household_variable_wrong_entity":
        message = (
            f"Household variable `{name}` belongs on `{expected_entity_plural}`, "
            f"not `{actual_entity_plural}`, at `{path}`."
        )
    elif input_type == "household_axis_variable":
        message = f"Unrecognized household axis variable `{name}` at `{path}`."
    elif input_type == "policy_parameter":
        message = f"Unrecognized policy parameter `{name}` at `{path}`."
    else:
        message = f"Unrecognized household variable `{name}` at `{path}`."
    return HouseholdInputError(
        type=input_type,
        name=name,
        path=path,
        message=message,
        expected_entity_plural=expected_entity_plural,
        actual_entity_plural=actual_entity_plural,
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
        if entity_plural == "axes":
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
            if not isinstance(entity_inputs, dict):
                continue
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
                    expected_entity_plural=expected_entity.plural,
                    actual_entity_plural=entity.plural,
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
    """Find policy parameter paths the country model does not have."""
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
) -> list[tuple[str, str, str, str]]:
    """List the (entity plural, entity id, variable, period) values to calculate.

    A value is requested when it is None or, with ``include_provided_values``,
    whatever it is. ``variable_names`` keeps only those variables. Values are
    listed in document order, as the Public API lists them.
    """
    computations: list[tuple[str, str, str, str]] = []
    for entity_plural, entities in situation.items():
        if not isinstance(entities, dict):
            continue
        for entity_id, variables in entities.items():
            if not isinstance(variables, dict):
                continue
            for variable_name, periods in variables.items():
                if not isinstance(periods, dict):
                    continue
                if variable_names is not None and variable_name not in variable_names:
                    continue
                computations.extend(
                    (entity_plural, entity_id, variable_name, str(period))
                    for period, value in periods.items()
                    if include_provided_values or value is None
                )
    return computations
