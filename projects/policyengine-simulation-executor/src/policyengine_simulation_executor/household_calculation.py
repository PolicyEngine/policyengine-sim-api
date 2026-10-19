"""Household calculations for the v2 Modal household functions.

``calculate_household`` runs one ``HouseholdExecutionRequest``: it calculates
the household under each requested policy, in order, and returns a
``HouseholdExecutionResult`` or a typed ``HouseholdExecutionFailure``.

The calculation reproduces the Public API's in-process household calculation
(its household calculation service and country module): the same input
preparation, simulation construction, reform value conversion, requested
values, value serialization, handling of values that cannot be calculated,
warnings and SPM receipt. Failures carry the codes and messages of the
Public API's matching error responses. This keeps v2 results comparable with
the Public API's results while both exist.
"""

from __future__ import annotations

import copy
import importlib
import inspect
import json
import logging
import math
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from datetime import UTC, date, datetime
from functools import lru_cache
from typing import TYPE_CHECKING, Any, Protocol

from policyengine_core.enums import Enum
from policyengine_core.errors import SituationParsingError
from policyengine_core.parameters import get_parameter
from policyengine_core.periods import instant
from policyengine_simulation_contract.household_execution import (
    CalculatedHousehold,
    HouseholdBundleProvenance,
    HouseholdCalculationError,
    HouseholdCalculationOutput,
    HouseholdExecutionFailure,
    HouseholdExecutionResult,
    HouseholdPolicyResult,
)
from policyengine_simulation_contract.spm import SPMInputError
from policyengine_simulation_observability.stages import V2_HOUSEHOLD_STAGES, Stage

from policyengine_simulation_executor.household_inputs import (
    HouseholdModelMetadata,
    add_missing_variables,
    drop_deprecated_inputs,
    find_unrecognized_household_inputs,
    find_unrecognized_policy_inputs,
    requested_computations,
    unrecognized_inputs_message,
)
from policyengine_simulation_executor.household_spm import (
    HOUSEHOLD_SPM_INPUT_ERROR_CODES,
    household_spm_error_detail,
    household_spm_provenance,
    resolve_household_spm_selection,
)
from policyengine_simulation_executor.stage12_bundle import load_stage12_bundle

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from types import ModuleType

    from policyengine_simulation_contract.household_execution import (
        HouseholdErrorCode,
        HouseholdExecutionRequest,
        HouseholdPolicy,
        HouseholdWorkerIdentity,
    )

logger = logging.getLogger(__name__)


class SpanFactory(Protocol):
    """Opens a named tracing span with attributes, as the observability runtime does."""

    def __call__(
        self, name: str, attributes: Mapping[str, object]
    ) -> AbstractContextManager[object]:
        """Return a context manager that measures the work inside it."""
        ...


def _no_span(
    name: str, attributes: Mapping[str, object]
) -> AbstractContextManager[object]:  # noqa: ARG001  # matches SpanFactory; records nothing
    return nullcontext()


@dataclass(frozen=True)
class CountryHouseholdModel:
    """A loaded country model package with its tax-benefit system and metadata."""

    country: str
    package: ModuleType | Any
    tax_benefit_system: Any
    metadata: HouseholdModelMetadata


@lru_cache(maxsize=4)
def load_country_household_model(country: str) -> CountryHouseholdModel:
    """Import ``country``'s model package and build its tax-benefit system once.

    The package is the one the installed PolicyEngine.py bundle names for the
    country. The result is kept for the life of the process, so warm Modal
    containers reuse it.
    """
    bundle = load_stage12_bundle().bundle
    country_bundle = next(item for item in bundle.countries if item.country == country)
    package = importlib.import_module(
        country_bundle.country_package_name.replace("-", "_")
    )
    system = package.CountryTaxBenefitSystem()
    return CountryHouseholdModel(
        country=country,
        package=package,
        tax_benefit_system=system,
        metadata=HouseholdModelMetadata.from_tax_benefit_system(system),
    )


def installed_household_bundle(country: str) -> HouseholdBundleProvenance:
    """Return the provenance of the installed bundle for ``country``."""
    resolved = load_stage12_bundle()
    return HouseholdBundleProvenance.from_bundle(
        resolved.bundle,
        bundle_manifest_sha256=resolved.bundle_manifest_sha256,
        country=country,
    )


class _CalculationFailedError(Exception):
    """A household calculation ended with a typed error."""

    def __init__(self, error: HouseholdCalculationError) -> None:
        super().__init__(error.message)
        self.error = error


def _error(
    code: HouseholdErrorCode,
    message: str,
    **details: object,
) -> _CalculationFailedError:
    return _CalculationFailedError(
        HouseholdCalculationError.model_validate(
            {"code": code, "message": message, **details}
        )
    )


def _spm_failure(
    error: BaseException, *, policy_key: str | None = None
) -> _CalculationFailedError | None:
    detail = household_spm_error_detail(error)
    if detail is None:
        return None
    return _error(
        "invalid_spm_selection",
        detail.message,
        spm_error=detail.model_dump(),
        policy_key=policy_key,
    )


def _serialize_float(value: object) -> float | str:
    """Serialize a number as the Public API does, naming non-finite values.

    Infinities become "Infinity" and "-Infinity" as in the Public API; an
    undefined number becomes "NaN", because the contract carries only finite
    numbers.
    """
    serialized = float(str(value))
    if math.isinf(serialized):
        return "Infinity" if serialized > 0 else "-Infinity"
    if math.isnan(serialized):
        return "NaN"
    return serialized


def _serialize_scalar(value: object) -> object:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def _serialize_axis_result(
    result: Any,  # noqa: ANN401  # a NumPy or EnumArray result from the country model
    variable: Any,  # noqa: ANN401  # a country model's Variable
    entity_index: int,
    count_entities: int,
) -> list[object]:
    if variable.value_type == Enum:
        result = result.decode_to_str()
    entity_values: list[object] = (
        result.reshape((-1, count_entities)).T[entity_index].tolist()
    )
    if variable.value_type is float:
        return [_serialize_float(value) for value in entity_values]
    if variable.value_type is str:
        return [str(value) for value in entity_values]
    return [_serialize_scalar(value) for value in entity_values]


def _serialize_entity_result(
    result: Any,  # noqa: ANN401  # a NumPy or EnumArray result from the country model
    variable: Any,  # noqa: ANN401  # a country model's Variable
    entity_index: int,
) -> object:
    if variable.value_type == Enum:
        return result.decode()[entity_index].name
    if variable.value_type is float:
        return _serialize_float(result[entity_index])
    if variable.value_type is str:
        return str(result[entity_index])
    return _serialize_scalar(result.tolist()[entity_index])


def _normalize_reform_values(
    reform: Mapping[str, Mapping[str, object]],
    system: Any,  # noqa: ANN401  # a country model's TaxBenefitSystem
) -> dict[str, dict[str, object]]:
    """Convert reform values to each parameter's current value type.

    Integers become floats, and numeric text becomes a number first, as in the
    Public API.
    """
    normalized: dict[str, dict[str, object]] = {}
    for parameter_name, parameter_updates in reform.items():
        parameter = get_parameter(system.parameters, parameter_name)
        normalized[parameter_name] = {}
        for time_period, value in parameter_updates.items():
            node_type = type(parameter.values_list[-1].value)
            if node_type is int:
                node_type = float
            converted: Any = value
            try:
                converted = float(converted)
            except (TypeError, ValueError, OverflowError):
                pass
            normalized[parameter_name][time_period] = node_type(converted)
    return normalized


def _apply_reform_to_system(
    system: Any,  # noqa: ANN401  # a country model's TaxBenefitSystem
    reform: Mapping[str, Mapping[str, object]],
) -> None:
    for parameter_name, parameter_updates in reform.items():
        parameter = get_parameter(system.parameters, parameter_name)
        for time_period, value in parameter_updates.items():
            start_instant, end_instant = time_period.split(".")
            parameter.update(
                start=instant(start_instant),
                stop=instant(end_instant),
                value=value,
            )


def _create_simulation(
    model: CountryHouseholdModel,
    situation: dict[str, Any],
    reform: Mapping[str, Mapping[str, object]],
    spm_selection: Mapping[str, object] | None,
) -> tuple[Any, Any]:
    """Build the country model's simulation of ``situation`` under ``reform``.

    A country ``Simulation`` that takes a tax-benefit system gets a reformed
    copy of the model's system; any other gets the converted reform values.
    """
    normalized_reform = None
    if reform:
        system = model.tax_benefit_system.clone()
        normalized_reform = _normalize_reform_values(reform, system)
    else:
        system = model.tax_benefit_system
    simulation_type = model.package.Simulation
    spm_options = {"spm": dict(spm_selection)} if spm_selection is not None else {}
    if "tax_benefit_system" in inspect.signature(simulation_type).parameters:
        if normalized_reform:
            _apply_reform_to_system(system, normalized_reform)
        simulation = simulation_type(
            tax_benefit_system=system, situation=situation, **spm_options
        )
        return simulation, simulation.tax_benefit_system
    simulation_options: dict[str, object] = {"situation": situation, **spm_options}
    if normalized_reform:
        simulation_options["reform"] = normalized_reform
    simulation = simulation_type(**simulation_options)
    return simulation, simulation.tax_benefit_system


def _entity_position(household: Mapping[str, Any], plural: str, entity_id: str) -> int:
    return list(household[plural]).index(entity_id)


@dataclass
class _PolicyCalculation:
    """One policy's simulation and the household its values are written into."""

    simulation: Any
    system: Any
    household: dict[str, Any]
    has_axes: bool
    spm_requested: bool
    warnings: list[str]

    def calculate_all(self) -> None:
        """Calculate every requested value, as the Public API's country module does."""
        computations = requested_computations(
            self.household,
            include_provided_values=self.has_axes,
            variable_names=set(self.system.variables) if self.has_axes else None,
        )
        for entity_plural, entity_id, variable_name, period in computations:
            population = self.simulation.get_population(entity_plural)
            try:
                self._calculate_value(
                    population, entity_plural, entity_id, variable_name, period
                )
            except Exception as error:  # noqa: BLE001  # any model error leaves this value uncalculated, as in the Public API
                detail = household_spm_error_detail(error)
                # A chosen measurement reports its missing inputs. An
                # inherited one leaves its dependants uncalculated, but only
                # for a missing input: a configuration failure always ends
                # the calculation.
                if detail is not None and (
                    self.spm_requested
                    or detail.code not in HOUSEHOLD_SPM_INPUT_ERROR_CODES
                ):
                    raise
                self._record_failure(
                    population, entity_plural, entity_id, variable_name, period, error
                )

    def _calculate_value(
        self,
        population: Any,  # noqa: ANN401  # a country model's Population
        entity_plural: str,
        entity_id: str,
        variable_name: str,
        period: str,
    ) -> None:
        variable = self.system.get_variable(variable_name)
        result = self.simulation.calculate(variable_name, period)
        cells = self.household[entity_plural][entity_id][variable_name]
        if self.has_axes:
            cells[period] = _serialize_axis_result(
                result,
                variable,
                _entity_position(self.household, entity_plural, entity_id),
                len(self.household[entity_plural]),
            )
            return
        cells[period] = _serialize_entity_result(
            result, variable, population.get_index(entity_id)
        )

    def _record_failure(  # noqa: PLR0913  # the Public API's failure record takes each coordinate
        self,
        population: Any,  # noqa: ANN401  # a country model's Population
        entity_plural: str,
        entity_id: str,
        variable_name: str,
        period: str,
        error: Exception,
    ) -> None:
        cells = self.household[entity_plural][entity_id][variable_name]
        if self.has_axes:
            axis_point_count = population.count // len(self.household[entity_plural])
            cells[period] = [None] * axis_point_count
            warning = (
                f"Unable to calculate {variable_name} for {entity_id} in "
                f"{period}; returned {axis_point_count} null axis values: {error}"
            )
            self.warnings.append(warning)
            logger.warning("%s", warning)
            return
        cells[period] = None
        logger.warning(
            "Unable to calculate %s for %s in %s: %s",
            variable_name,
            entity_id,
            period,
            error,
        )


def _calculate_policy(  # noqa: PLR0913  # each input of one policy's calculation is distinct
    model: CountryHouseholdModel,
    situation: Mapping[str, Any],
    policy: HouseholdPolicy,
    *,
    spm_selection: Mapping[str, object] | None,
    spm_requested: bool,
    input_warnings: tuple[str, ...],
) -> HouseholdPolicyResult:
    """Calculate the household under one policy, raising a typed failure."""
    policy_key = policy.policy_key
    try:
        simulation, system = _create_simulation(
            model, copy.deepcopy(dict(situation)), policy.parameters, spm_selection
        )
        calculation = _PolicyCalculation(
            simulation=simulation,
            system=system,
            household=json.loads(json.dumps(situation)),
            has_axes="axes" in situation,
            spm_requested=spm_requested,
            warnings=[],
        )
        calculation.calculate_all()
        spm_provenance = household_spm_provenance(simulation, spm_selection)
    except SituationParsingError as error:
        failure = _spm_failure(error, policy_key=policy_key) or _error(
            "invalid_household",
            f"Invalid household payload: {error}",
            policy_key=policy_key,
        )
        raise failure from error
    except Exception as error:
        failure = _spm_failure(error, policy_key=policy_key)
        if failure is None:
            logger.exception("Error calculating household under policy %s", policy_key)
            failure = _error(
                "calculation_failed",
                f"Error calculating household under policy: {error}",
                policy_key=policy_key,
            )
        raise failure from error
    return HouseholdPolicyResult(
        policy_key=policy_key,
        household=CalculatedHousehold.model_validate(calculation.household),
        warnings=(*input_warnings, *calculation.warnings),
        spm_provenance=spm_provenance,
    )


def _prepared_situation(
    request: HouseholdExecutionRequest,
    model: CountryHouseholdModel,
    *,
    today: date,
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Prepare the household as the Public API does; raise for unknown names."""
    situation = request.household.to_situation()
    if request.add_missing_variables:
        add_missing_variables(situation, model.metadata, today=today)
    deprecated = drop_deprecated_inputs(situation)
    household_errors = find_unrecognized_household_inputs(
        deprecated.household, model.metadata
    )
    for policy in request.policies:
        policy_errors = find_unrecognized_policy_inputs(
            policy.parameters, model.metadata
        )
        input_errors = [*household_errors, *policy_errors]
        if input_errors:
            failure = _error(
                "invalid_inputs",
                unrecognized_inputs_message(input_errors),
                input_errors=[error.model_dump() for error in input_errors],
                policy_key=policy.policy_key if policy_errors else None,
            )
            raise failure
    return deprecated.household, tuple(
        warning.message for warning in deprecated.warnings
    )


def _calculate_output(
    request: HouseholdExecutionRequest,
    bundle: HouseholdBundleProvenance,
    *,
    model_loader: Callable[[str], CountryHouseholdModel],
    today: date,
    span: SpanFactory,
) -> HouseholdCalculationOutput:
    """Calculate every policy of ``request``, raising a typed failure."""
    if request.bundle != bundle:
        mismatch = _error(
            "bundle_mismatch",
            "This household function runs PolicyEngine.py "
            f"{bundle.policyengine_version} with {bundle.country_package_name} "
            f"{bundle.country_package_version} "
            f"(bundle {bundle.bundle_manifest_sha256}), not the requested bundle",
        )
        raise mismatch
    try:
        spm_selection = resolve_household_spm_selection(
            request.country,
            request.spm,
            saved_household=request.spm_selection_source == "saved_household",
        )
    except SPMInputError as error:
        failure = _error(
            "invalid_spm_selection",
            error.message,
            spm_error={"code": error.code, "message": error.message},
        )
        raise failure from error
    country_attributes = {"country": request.country}
    with span(
        V2_HOUSEHOLD_STAGES.name(Stage.V2_HOUSEHOLD_COUNTRY_MODEL_LOAD),
        country_attributes,
    ):
        model = model_loader(request.country)
    with span(
        V2_HOUSEHOLD_STAGES.name(Stage.V2_HOUSEHOLD_INPUT_NORMALIZATION),
        country_attributes,
    ):
        situation, input_warnings = _prepared_situation(request, model, today=today)
    results = []
    for policy in request.policies:
        with span(
            V2_HOUSEHOLD_STAGES.name(Stage.V2_HOUSEHOLD_CALCULATION),
            {**country_attributes, "policy_key": policy.policy_key},
        ):
            results.append(
                _calculate_policy(
                    model,
                    situation,
                    policy,
                    spm_selection=spm_selection,
                    spm_requested=request.spm_requested,
                    input_warnings=input_warnings,
                )
            )
    return HouseholdCalculationOutput(
        country=request.country, results=tuple(results), bundle=bundle
    )


def calculate_household(  # noqa: PLR0913  # the seams tests and the Modal function replace are keyword-only
    request: HouseholdExecutionRequest,
    *,
    worker: HouseholdWorkerIdentity,
    installed_bundle: HouseholdBundleProvenance | None = None,
    model_loader: Callable[[str], CountryHouseholdModel] = load_country_household_model,
    today: date | None = None,
    span: SpanFactory = _no_span,
) -> HouseholdExecutionResult | HouseholdExecutionFailure:
    """Calculate one household under each requested policy.

    Returns the per-policy results, or a typed failure for a request for
    another bundle, an unusable SPM selection, unrecognized inputs, a household
    the country model cannot build, or any other calculation error. Checks run
    in the Public API's order: SPM selection, inputs, then each policy.
    ``today`` sets the default household year (the current UTC date when
    omitted).
    """
    bundle = installed_bundle or installed_household_bundle(request.country)
    try:
        output = _calculate_output(
            request,
            bundle,
            model_loader=model_loader,
            today=today or datetime.now(UTC).date(),
            span=span,
        )
    except _CalculationFailedError as failure:
        return HouseholdExecutionFailure(
            calculation_id=request.calculation_id,
            error=failure.error,
            worker=worker,
        )
    return HouseholdExecutionResult(
        calculation_id=request.calculation_id,
        output=output,
        worker=worker,
    )
