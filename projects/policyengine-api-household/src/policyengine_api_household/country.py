"""Country calculation engine for the household calculation experiment.

Recovered (with adaptation) from PR #49 (commit 019b808):
libs/policyengine-api/src/policyengine_api/api/country.py

Adaptations vs. the original:
* Only the calculation path is kept — the metadata/parameter/region
  builders from PR #49 are out of scope for this experiment.
* Countries are loaded lazily (loading a country tax-benefit system is
  slow, so we only pay for the countries actually used).
* ``calculate`` operates on the raw household dict (like the v1
  household API does) rather than a Pydantic model, so the response
  echoes back exactly the entity groups the caller sent.
* Only ``us`` and ``uk`` are supported.

Engine choice: country model packages (``policyengine_us`` /
``policyengine_uk``) are used directly via
``<package>.Simulation(situation=...)`` — the same engine the v1
household API and PR #49 use, which is what gives byte-level parity a
chance. Versions are pinned in pyproject.toml to match
projects/policyengine-api-simulation's dependency set.
"""

import importlib
import importlib.metadata
import logging
import math
from copy import deepcopy
from typing import Any, Union

import dpath.util
from numpy.typing import ArrayLike
from policyengine_core.model_api import Enum
from policyengine_core.parameters import Parameter as CoreParameter
from policyengine_core.parameters import get_parameter
from policyengine_core.periods import instant
from policyengine_core.populations import Population
from policyengine_core.simulations import Simulation
from policyengine_core.taxbenefitsystems import TaxBenefitSystem
from policyengine_core.variables import Variable

logger = logging.getLogger(__name__)

# PyPI distribution names, used for the response's model-version block.
COUNTRY_PACKAGE_DISTRIBUTIONS = {
    "us": "policyengine-us",
    "uk": "policyengine-uk",
}

# Importable module names for each supported country.
COUNTRY_PACKAGE_MODULES = {
    "us": "policyengine_us",
    "uk": "policyengine_uk",
}


class PolicyEngineCountry:
    def __init__(self, country_package_name: str, country_id: str):
        self.country_package_name = country_package_name
        self.country_id = country_id
        self.country_package = importlib.import_module(country_package_name)
        self.tax_benefit_system: TaxBenefitSystem = (
            self.country_package.CountryTaxBenefitSystem()
        )

    def calculate(
        self,
        household: dict[str, Any],
        reform: Union[dict, None] = None,
    ) -> dict[str, Any]:
        """Fill every null leaf of the household with its computed value.

        ``household`` follows the v1 entity structure
        (entity_group -> entity -> variable -> period -> value); a null
        value requests computation of that variable for that period.
        """
        system: TaxBenefitSystem = self._prepare_tax_benefit_system(reform)

        # ``axes`` is understood by the core simulation builder but is
        # not an entity group; keep it in the situation, but never treat
        # it as a computation target.
        #
        # PR #49 constructed simulations as
        # ``Simulation(tax_benefit_system=..., situation=...)``, which
        # still works for policyengine-us. policyengine-uk >= 2.x
        # redefined ``Simulation.__init__`` without the
        # ``tax_benefit_system`` keyword, so fall back to
        # ``Simulation(situation=...)`` there.
        try:
            simulation: Simulation = self.country_package.Simulation(
                tax_benefit_system=system,
                situation=household,
            )
        except TypeError:
            if reform:
                raise NotImplementedError(
                    f"Parametric reforms are not supported for "
                    f"{self.country_package_name} in this experiment."
                )
            simulation = self.country_package.Simulation(
                situation=household,
            )
            system = simulation.tax_benefit_system

        household_result: dict[str, Any] = deepcopy(household)
        requested_computations: list[tuple[str, str, str, str]] = (
            get_requested_computations(household)
        )

        for computation in requested_computations:
            self._process_computation(simulation, system, household_result, computation)

        return household_result

    def _prepare_tax_benefit_system(
        self, reform: Union[dict, None] = None
    ) -> TaxBenefitSystem:
        """Prepare the tax benefit system with optional reforms applied."""
        if not reform:
            return self.tax_benefit_system

        system: TaxBenefitSystem = self.tax_benefit_system.clone()

        for parameter_name, periods in reform.items():
            for time_period, value in periods.items():
                self._update_parameter(system, parameter_name, time_period, value)

        return system

    def _update_parameter(
        self,
        system: TaxBenefitSystem,
        parameter_name: str,
        time_period: str,
        value: Any,
    ) -> None:
        """Update a specific parameter in the tax benefit system."""
        start_instant, end_instant = time_period.split(".")
        parameter: CoreParameter = get_parameter(system.parameters, parameter_name)

        # Determine the appropriate type for the value
        node_type = type(parameter.values_list[-1].value)

        # Cast int to float to harmonize numeric handling
        # Int-float casting copied/pasted from the original household API at
        # https://github.com/PolicyEngine/policyengine-household-api/blob/96ebe4440f9cba81b09f64d53aa4f7e6e7003d77/policyengine_household_api/country.py#L319
        if node_type is int:
            node_type = float

        # Convert value to float if possible
        try:
            value = float(value)
        except (ValueError, TypeError):
            pass

        parameter.update(
            start=instant(start_instant),
            stop=instant(end_instant),
            value=node_type(value),
        )

    def _process_computation(
        self,
        simulation: Simulation,
        system: TaxBenefitSystem,
        household: dict[str, Any],
        computation,
    ):
        """Process a single computation request and update the household result."""
        entity_plural, entity_id, variable_name, period = computation

        try:
            variable: Variable = system.get_variable(variable_name)
            result: ArrayLike = simulation.calculate(variable_name, period)

            if household.get("axes"):
                self._handle_axes_computation(
                    household,
                    entity_plural,
                    entity_id,
                    variable_name,
                    period,
                    result,
                )
            else:
                self._handle_single_computation(
                    simulation,
                    household,
                    entity_plural,
                    entity_id,
                    variable_name,
                    period,
                    result,
                    variable,
                )
        except Exception as e:
            self._handle_computation_error(
                household, entity_plural, entity_id, variable_name, period, e
            )

    def _handle_axes_computation(
        self,
        household: dict[str, Any],
        entity_plural: str,
        entity_id,
        variable_name: str,
        period,
        result,
    ) -> None:
        """Handle computation for households with axes."""
        count_entities: int = len(household[entity_plural])
        entity_index: int = self._find_entity_index(household, entity_plural, entity_id)

        # Reshape result and get values for the specific entity
        result_values: list[float] = (
            result.astype(float).reshape((-1, count_entities)).T[entity_index].tolist()
        )

        # Check for infinite values
        if any(math.isinf(value) for value in result_values):
            raise ValueError("Infinite value")

        # Update household with results
        household[entity_plural][entity_id][variable_name][period] = result_values

    def _find_entity_index(
        self, household: dict[str, Any], entity_plural: str, entity_id
    ):
        """Find the index of an entity within its plural group."""
        entity_index = 0

        _entity_id: str
        for _entity_id in household[entity_plural].keys():
            if _entity_id == entity_id:
                break
            entity_index += 1
        return entity_index

    def _handle_single_computation(
        self,
        simulation: Simulation,
        household: dict[str, Any],
        entity_plural: str,
        entity_id,
        variable_name: str,
        period,
        result: ArrayLike,
        variable: Variable,
    ):
        """Handle computation for a single entity."""
        population: Population = simulation.get_population(entity_plural)
        entity_index: int = population.get_index(entity_id)

        # Format the result based on variable type
        entity_result = self._format_result(result, entity_index, variable)

        # Update household with result
        household[entity_plural][entity_id][variable_name][period] = entity_result

    def _format_result(
        self, result: ArrayLike, entity_index, variable: Variable
    ) -> Any:
        """Format calculation result based on variable type."""
        if variable.value_type is Enum:
            return result.decode()[entity_index].name
        if variable.value_type is float:
            value = float(str(result[entity_index]))
            # Convert infinities to JSON-compatible strings
            if value == float("inf"):
                return "Infinity"
            if value == float("-inf"):
                return "-Infinity"
            return value
        if variable.value_type is str:
            return str(result[entity_index])
        return result.tolist()[entity_index]

    def _handle_computation_error(
        self,
        household: dict[str, Any],
        entity_plural: str,
        entity_id: str,
        variable_name: str,
        period,
        error,
    ):
        """Handle errors during computation."""

        # Original code passes if "axes" in household - why?
        if "axes" not in household:
            household[entity_plural][entity_id][variable_name][period] = None
            logger.warning(
                "Error computing %s for %s: %s",
                variable_name,
                entity_id,
                error,
            )


def get_requested_computations(household: dict[str, Any]):
    requested_computations = dpath.util.search(
        household,
        "*/*/*/*",
        afilter=lambda t: t is None,
        yielded=True,
    )
    requested_computation_data = []

    for computation in requested_computations:
        path = computation[0]
        entity_plural, entity_id, variable_name, period = path.split("/")
        requested_computation_data.append(
            (entity_plural, entity_id, variable_name, period)
        )

    return requested_computation_data


_COUNTRIES: dict[str, PolicyEngineCountry] = {}


def get_country(country_id: str) -> PolicyEngineCountry:
    """Lazily construct (and cache) the country engine."""
    if country_id not in _COUNTRIES:
        _COUNTRIES[country_id] = PolicyEngineCountry(
            COUNTRY_PACKAGE_MODULES[country_id], country_id
        )
    return _COUNTRIES[country_id]


def get_model_version(country_id: str) -> str:
    """Version of the country model package used for calculations."""
    return importlib.metadata.version(COUNTRY_PACKAGE_DISTRIBUTIONS[country_id])
