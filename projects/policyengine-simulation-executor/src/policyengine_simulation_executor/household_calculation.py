"""Household calculations for the v2 Modal household functions.

``calculate_household`` runs one ``HouseholdExecutionRequest``: it calculates
the household under each requested policy, in order, and returns a
``HouseholdExecutionResult`` or a typed ``HouseholdExecutionFailure``.

The calculation reproduces the Public API's in-process household calculation
(its household calculation service, household routes and country module): the
same input preparation, simulation construction, reform value conversion,
requested values, value formatting, handling of values that cannot be
calculated, warnings and SPM receipt. Failures carry the codes and messages of
the Public API's matching error responses. This keeps v2 results comparable
with the Public API's results while both exist.

Every known difference from the Public API is listed here:

- An undefined number is the string "NaN": the contract carries only finite
  numbers and JSON scalars, and the Public API writes a bare ``NaN``, which
  is not JSON. A date is the HTTP date text the Public API's Flask encoder
  writes.
- Missing variables are added for the request's ``year``. The Public API
  adds them for the first period of the age of a person named ``you``, or
  else its host's current year (and fails with an internal error when that
  age is one value, not by period); v2 households name people by record ID.
- A variable given as one value, not by period, is that value for ``year``,
  and an axis without a period varies ``year``; the calculated household
  shows both with that period. The Public API leaves them to the country
  model's own default year (2024 for the US, 2025 for the UK), and does not
  calculate a variable given as one null value.
- An explicit null ``axes`` means no axes, because the contract drops it; the
  Public API treats the key's presence as a household with axes.
- Every policy's parameters are checked before any policy is calculated, so
  an unknown parameter in a reform fails the whole request. Likewise any
  failure under one policy, such as a reform value that cannot be converted
  or an SPM error only the reform causes, returns one failure for the whole
  request, without the policies already calculated; the Public API's
  separate request for each policy would still return the others.
- Its messages are the request routes' (``/calculate`` and
  ``/calculate-full``). The stored-household route words a household it
  cannot read, and any other failure except an SPM error, with the household
  and policy IDs, which a household function does not have.
- A request the contract's models reject, such as a group without
  ``members``, never reaches this module; the caller reports it.
"""

from __future__ import annotations

import importlib
import json
import logging
import math
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from email.utils import format_datetime
from functools import lru_cache
from typing import TYPE_CHECKING, Any

from policyengine_core.enums import Enum
from policyengine_core.errors import SituationParsingError
from policyengine_core.parameters import get_parameter
from policyengine_core.periods import period as parse_period
from policyengine_simulation_contract.household_execution import (
    CalculatedHousehold,
    HouseholdBundleProvenance,
    HouseholdCalculationError,
    HouseholdCalculationOutput,
    HouseholdExecutionFailure,
    HouseholdExecutionResult,
    HouseholdPolicyResult,
)
from policyengine_simulation_contract.spm import (
    SPMErrorDetail,
    SPMInputError,
    SPMResolvedConfiguration,
)
from policyengine_simulation_observability.stages import V2_HOUSEHOLD_STAGES, Stage

from policyengine_simulation_executor.household_inputs import (
    HouseholdModelMetadata,
    RequestedValue,
    add_missing_variables,
    drop_deprecated_inputs,
    find_unrecognized_household_inputs,
    find_unrecognized_policy_inputs,
    requested_computations,
    unrecognized_inputs_message,
    with_request_year,
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

    from policyengine_core.periods import Period
    from policyengine_core.simulations import Simulation
    from policyengine_core.taxbenefitsystems import TaxBenefitSystem
    from policyengine_observability import ObservabilityRuntime
    from policyengine_simulation_contract.household_execution import (
        HouseholdErrorCode,
        HouseholdExecutionRequest,
        HouseholdPolicy,
        HouseholdWorkerIdentity,
    )
    from policyengine_simulation_contract.spm import SPMProvenance

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CountryHouseholdModel:
    """A loaded country model: its ``Simulation`` type, tax-benefit system and metadata.

    The tax-benefit system supplies the metadata and the value types of
    reform parameters; simulations build their own, as in the Public API.
    """

    country: str
    simulation_type: Callable[..., Simulation]
    tax_benefit_system: TaxBenefitSystem
    metadata: HouseholdModelMetadata


@lru_cache(maxsize=4)
def load_country_household_model(country: str) -> CountryHouseholdModel:
    """Import ``country``'s model package and build its tax-benefit system once.

    The package is the one the installed PolicyEngine.py bundle names for the
    country. The result is kept for the life of the process, so warm Modal
    containers reuse it. Raises ValueError for a country the bundle lacks.
    """
    bundle = load_stage12_bundle().bundle
    country_bundle = next(
        (item for item in bundle.countries if item.country == country), None
    )
    if country_bundle is None:
        msg = f"the installed bundle does not carry country {country!r}"
        raise ValueError(msg)
    package = importlib.import_module(
        country_bundle.country_package_name.replace("-", "_")
    )
    system = package.CountryTaxBenefitSystem()
    return CountryHouseholdModel(
        country=country,
        simulation_type=package.Simulation,
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


@dataclass(frozen=True)
class HouseholdCalculationDependencies:
    """Seams a household calculation uses; unset seams use the defaults.

    The defaults are the installed bundle, the process's cached country
    models and no telemetry runtime.
    """

    installed_bundle: HouseholdBundleProvenance | None = None
    model_loader: Callable[[str], CountryHouseholdModel] = load_country_household_model
    runtime: ObservabilityRuntime | None = None


class _CalculationFailedError(Exception):
    """A household calculation ended with a typed error."""

    def __init__(self, error: HouseholdCalculationError) -> None:
        super().__init__(error.message)
        self.error = error


def _failure(
    code: HouseholdErrorCode,
    message: str,
    *,
    policy_key: str | None = None,
) -> _CalculationFailedError:
    return _CalculationFailedError(
        HouseholdCalculationError(code=code, message=message, policy_key=policy_key)
    )


def _spm_failure(
    error: BaseException, *, policy_key: str | None
) -> _CalculationFailedError | None:
    detail = household_spm_error_detail(error)
    if detail is None:
        return None
    return _CalculationFailedError(
        HouseholdCalculationError(
            code="invalid_spm_selection",
            message=detail.message,
            spm_error=detail,
            policy_key=policy_key,
        )
    )


def _invalid_household_failure(
    error: SituationParsingError, *, policy_key: str
) -> _CalculationFailedError:
    """Report a household the country model cannot read, as the Public API does."""
    return _failure(
        "invalid_household",
        f"Invalid household payload: {error}",
        policy_key=policy_key,
    )


def _bundle_mismatch_failure(
    bundle: HouseholdBundleProvenance,
) -> _CalculationFailedError:
    """Report a request for a bundle other than ``bundle``, the one installed."""
    return _failure(
        "bundle_mismatch",
        "This household function runs PolicyEngine.py "
        f"{bundle.policyengine_version} with {bundle.country_package_name} "
        f"{bundle.country_package_version} "
        f"(bundle {bundle.bundle_manifest_sha256}), not the requested bundle",
    )


def _unexpected_failure(
    error: BaseException, *, policy_key: str
) -> _CalculationFailedError:
    """Report an SPM error as such, and anything else as a failed calculation."""
    failure = _spm_failure(error, policy_key=policy_key)
    if failure is not None:
        return failure
    logger.error(
        "Error calculating household under policy %s", policy_key, exc_info=error
    )
    return _failure(
        "calculation_failed",
        f"Error calculating household under policy: {error}",
        policy_key=policy_key,
    )


def _stage_scope(
    runtime: ObservabilityRuntime | None,
    stage: Stage,
    attributes: Mapping[str, object],
) -> AbstractContextManager[object]:
    if runtime is None:
        return nullcontext()
    return runtime.span(V2_HOUSEHOLD_STAGES.name(stage), attributes=dict(attributes))


# Value formatting, as the Public API's country module and JSON encoder
# write values.


def _serialize_float(value: object) -> float | str:
    """Write a number as the Public API does, naming non-finite values."""
    serialized = float(str(value))
    if math.isinf(serialized):
        return "Infinity" if serialized > 0 else "-Infinity"
    if math.isnan(serialized):
        return "NaN"
    return serialized


def _serialize_scalar(value: object) -> object:
    """Write a date as the HTTP date text the Public API's Flask encoder writes."""
    if isinstance(value, datetime):
        moment = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return format_datetime(moment.astimezone(UTC), usegmt=True)
    if isinstance(value, date):
        return format_datetime(datetime.combine(value, time(), tzinfo=UTC), usegmt=True)
    return value


def _serialize_axis_values(
    result: Any,  # noqa: ANN401  # a NumPy array or EnumArray from the country model
    value_type: type,
    entity_index: int,
    entity_count: int,
) -> list[object]:
    if value_type == Enum:
        result = result.decode_to_str()
    entity_values: list[object] = (
        result.reshape((-1, entity_count)).T[entity_index].tolist()
    )
    if value_type is float:
        return [_serialize_float(value) for value in entity_values]
    if value_type is str:
        return [str(value) for value in entity_values]
    return [_serialize_scalar(value) for value in entity_values]


def _serialize_entity_value(
    result: Any,  # noqa: ANN401  # a NumPy array or EnumArray from the country model
    value_type: type,
    entity_index: int,
) -> object:
    if value_type == Enum:
        return result.decode()[entity_index].name
    if value_type is float:
        return _serialize_float(result[entity_index])
    if value_type is str:
        return str(result[entity_index])
    return _serialize_scalar(result.tolist()[entity_index])


@dataclass(frozen=True)
class _PreparedHousehold:
    """A household ready to calculate under each policy.

    ``situation`` has its missing variables added and deprecated inputs
    removed; ``input_warnings`` say which inputs were removed.
    ``spm_selection`` is the resolved SPM selection (None when no SPM
    applies) and ``spm_requested`` whether the caller chose it.
    """

    situation: dict[str, Any]
    input_warnings: tuple[str, ...]
    spm_selection: dict[str, Any] | None
    spm_requested: bool


def _normalized_reform(
    reform: Mapping[str, Mapping[str, object]],
    system: TaxBenefitSystem,
) -> dict[str, dict[str, object]]:
    """Convert reform values to each parameter's current value type.

    Integers become floats, and numeric text becomes a number first, as in the
    Public API.
    """
    root = system.parameters
    if root is None:
        msg = "the country model has no parameters"
        raise LookupError(msg)
    normalized: dict[str, dict[str, object]] = {}
    for parameter_name, parameter_updates in reform.items():
        parameter = get_parameter(root, parameter_name)
        convert = _reform_value_type(parameter.values_list[-1].value)
        normalized[parameter_name] = {
            time_period: convert(_numeric_if_possible(value))
            for time_period, value in parameter_updates.items()
        }
    return normalized


def _reform_value_type(current_value: object) -> Callable[[Any], object]:
    """Return the type a reform value becomes: the current value's, int as float."""
    value_type = type(current_value)
    return float if value_type is int else value_type


def _numeric_if_possible(value: object) -> object:
    """Return ``value`` as a float when it reads as one, as the Public API does."""
    if not isinstance(value, (bool, int, float, str)):
        return value
    try:
        return float(value)
    except (ValueError, OverflowError):
        return value


def _create_simulation(
    model: CountryHouseholdModel,
    prepared: _PreparedHousehold,
    policy: HouseholdPolicy,
) -> Simulation:
    """Build the country model's simulation of the household under ``policy``.

    The country ``Simulation`` takes the reform as converted parameter values
    and the resolved SPM selection, as the Public API passes them.
    """
    options: dict[str, object] = {
        "situation": json.loads(json.dumps(prepared.situation))
    }
    if prepared.spm_selection is not None:
        options["spm"] = dict(prepared.spm_selection)
    if policy.parameters:
        options["reform"] = _normalized_reform(
            policy.parameters, model.tax_benefit_system
        )
    return model.simulation_type(**options)


def _model_period(text: str) -> Period:
    """Read a household period key as policyengine-core's ``calculate`` reads it.

    Raises ValueError for text that names no period.
    """
    parsed = parse_period(text)
    if parsed is None:
        msg = f"{text!r} names no period"
        raise ValueError(msg)
    return parsed


@dataclass
class _PolicyCalculation:
    """One policy's simulation and the household its values are written into."""

    simulation: Simulation
    household: dict[str, Any]
    spm_requested: bool
    warnings: list[str] = field(default_factory=list)

    @property
    def has_axes(self) -> bool:
        """Whether the household is calculated at several axis points."""
        return "axes" in self.household

    def calculate_all(self) -> None:
        """Calculate every requested value, as the Public API's country module does."""
        system = self.simulation.tax_benefit_system
        computations = requested_computations(
            self.household,
            include_provided_values=self.has_axes,
            variable_names=set(system.variables) if self.has_axes else None,
        )
        for requested in computations:
            try:
                self._calculate_value(requested)
            except Exception as error:
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
                self._record_failure(requested, error)

    def _calculate_value(self, requested: RequestedValue) -> None:
        entity_plural, entity_id, variable_name, period = requested
        population = self.simulation.get_population(entity_plural)
        variable = self.simulation.tax_benefit_system.get_variable(variable_name)
        if variable is None:
            msg = f"the country model has no variable {variable_name}"
            raise LookupError(msg)
        value_type = variable.value_type
        result = self.simulation.calculate(variable_name, _model_period(period))
        entities = self.household[entity_plural]
        if self.has_axes:
            value: object = _serialize_axis_values(
                result, value_type, list(entities).index(entity_id), len(entities)
            )
        else:
            value = _serialize_entity_value(
                result, value_type, population.get_index(entity_id)
            )
        entities[entity_id][variable_name][period] = value

    def _record_failure(self, requested: RequestedValue, error: Exception) -> None:
        entity_plural, entity_id, variable_name, period = requested
        cells = self.household[entity_plural][entity_id][variable_name]
        if not self.has_axes:
            cells[period] = None
            logger.warning(
                "Unable to calculate %s for %s in %s: %s",
                variable_name,
                entity_id,
                period,
                error,
            )
            return
        population = self.simulation.get_population(entity_plural)
        axis_point_count = population.count // len(self.household[entity_plural])
        cells[period] = [None] * axis_point_count
        warning = (
            f"Unable to calculate {variable_name} for {entity_id} in "
            f"{period}; returned {axis_point_count} null axis values: {error}"
        )
        self.warnings.append(warning)
        logger.warning("%s", warning)


def _parsed_policy_calculation(
    model: CountryHouseholdModel,
    prepared: _PreparedHousehold,
    policy: HouseholdPolicy,
) -> _PolicyCalculation:
    """Build the policy's simulation, failing as the Public API's parse stage does."""
    try:
        simulation = _create_simulation(model, prepared, policy)
    except SituationParsingError as error:
        raise _invalid_household_failure(error, policy_key=policy.policy_key) from error
    except Exception as error:
        raise _unexpected_failure(error, policy_key=policy.policy_key) from error
    return _PolicyCalculation(
        simulation=simulation,
        household=json.loads(json.dumps(prepared.situation)),
        spm_requested=prepared.spm_requested,
    )


def _calculated_policy_result(
    calculation: _PolicyCalculation,
    prepared: _PreparedHousehold,
    policy: HouseholdPolicy,
) -> HouseholdPolicyResult:
    """Calculate the requested values and return the policy's result."""
    calculation.calculate_all()
    spm_provenance: SPMProvenance | None = household_spm_provenance(
        calculation.simulation, prepared.spm_selection
    )
    return HouseholdPolicyResult(
        policy_key=policy.policy_key,
        household=CalculatedHousehold.model_validate(calculation.household),
        warnings=(*prepared.input_warnings, *calculation.warnings),
        spm_provenance=spm_provenance,
    )


def _calculate_policy(
    model: CountryHouseholdModel,
    prepared: _PreparedHousehold,
    policy: HouseholdPolicy,
) -> HouseholdPolicyResult:
    """Calculate the household under one policy, raising a typed failure."""
    calculation = _parsed_policy_calculation(model, prepared, policy)
    try:
        return _calculated_policy_result(calculation, prepared, policy)
    except SituationParsingError as error:
        failure = _spm_failure(
            error, policy_key=policy.policy_key
        ) or _invalid_household_failure(error, policy_key=policy.policy_key)
        raise failure from error
    except Exception as error:
        raise _unexpected_failure(error, policy_key=policy.policy_key) from error


def _resolved_spm_selection(
    request: HouseholdExecutionRequest,
) -> dict[str, Any] | None:
    try:
        return resolve_household_spm_selection(
            request.country,
            request.spm,
            sent=request.spm_requested,
            saved_household=request.spm_selection_source == "saved_household",
        )
    except SPMInputError as error:
        raise _CalculationFailedError(
            HouseholdCalculationError(
                code="invalid_spm_selection",
                message=error.message,
                spm_error=SPMErrorDetail(code=error.code, message=error.message),
            )
        ) from error


def _prepared_household(
    request: HouseholdExecutionRequest,
    model: CountryHouseholdModel,
    *,
    spm_selection: dict[str, Any] | None,
) -> _PreparedHousehold:
    """Prepare the household as the Public API does; raise for unknown names.

    Unknown household names are reported with the first policy, as the Public
    API's baseline request would report them; unknown policy parameters are
    reported with the policy that names them.
    """
    situation = request.household.to_situation()
    with_request_year(situation, year=request.year)
    if request.add_missing_variables:
        add_missing_variables(situation, model.metadata, year=request.year)
    deprecated = drop_deprecated_inputs(situation)
    household_errors = find_unrecognized_household_inputs(
        deprecated.household, model.metadata
    )
    for policy in request.policies:
        policy_errors = find_unrecognized_policy_inputs(
            policy.parameters, model.metadata
        )
        input_errors = (*household_errors, *policy_errors)
        if input_errors:
            raise _CalculationFailedError(
                HouseholdCalculationError(
                    code="invalid_inputs",
                    message=unrecognized_inputs_message(input_errors),
                    input_errors=input_errors,
                    policy_key=policy.policy_key if policy_errors else None,
                )
            )
    return _PreparedHousehold(
        situation=deprecated.household,
        input_warnings=tuple(warning.message for warning in deprecated.warnings),
        spm_selection=spm_selection,
        spm_requested=request.spm_requested,
    )


def _calculate_output(
    request: HouseholdExecutionRequest,
    bundle: HouseholdBundleProvenance,
    dependencies: HouseholdCalculationDependencies,
) -> HouseholdCalculationOutput:
    """Calculate every policy of ``request``, raising a typed failure.

    After the bundle check and loading the country model, checks run in the
    Public API's order: the SPM selection, the household's and policies'
    names, then each policy. The model loads first so its span measures the
    whole load, which for the US otherwise happens while the SPM selection is
    resolved.
    """
    if request.bundle != bundle:
        raise _bundle_mismatch_failure(bundle)
    country = {"country": request.country}
    runtime = dependencies.runtime
    with _stage_scope(runtime, Stage.V2_HOUSEHOLD_COUNTRY_MODEL_LOAD, country):
        model = dependencies.model_loader(request.country)
    with _stage_scope(runtime, Stage.V2_HOUSEHOLD_INPUT_NORMALIZATION, country):
        spm_selection = _resolved_spm_selection(request)
        prepared = _prepared_household(request, model, spm_selection=spm_selection)
    results = []
    for policy in request.policies:
        attributes = {**country, "policy_key": policy.policy_key}
        with _stage_scope(runtime, Stage.V2_HOUSEHOLD_CALCULATION, attributes):
            results.append(_calculate_policy(model, prepared, policy))
    return HouseholdCalculationOutput(
        country=request.country,
        bundle=bundle,
        spm_config=(
            SPMResolvedConfiguration.model_validate(spm_selection)
            if spm_selection is not None
            else None
        ),
        results=tuple(results),
    )


def calculate_household(
    request: HouseholdExecutionRequest,
    *,
    worker: HouseholdWorkerIdentity,
    required_country: str | None = None,
    dependencies: HouseholdCalculationDependencies | None = None,
) -> HouseholdExecutionResult | HouseholdExecutionFailure:
    """Calculate one household under each requested policy.

    Returns the per-policy results, or a typed failure for a request for
    another bundle, an unusable SPM selection, unrecognized inputs, a household
    the country model cannot build, or any other calculation error. Raises
    ValueError when ``required_country`` is set and the request names another
    country; a household function calculates its own country only.
    """
    if required_country is not None and request.country != required_country:
        msg = f"this function calculates {required_country} households only"
        raise ValueError(msg)
    dependencies = dependencies or HouseholdCalculationDependencies()
    bundle = dependencies.installed_bundle or installed_household_bundle(
        request.country
    )
    try:
        output = _calculate_output(request, bundle, dependencies)
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
