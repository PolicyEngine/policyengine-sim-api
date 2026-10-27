"""The Modal calls the Simulation Entrypoint starts for v2 economy runs.

Each economy simulation run has its own Modal call, and each economy report
run's comparison step another (``simulation_runs`` and ``report_runs`` in the
persistence library):

- ``EconomySimulationCall`` asks a country's ``ECONOMY_SIMULATION_FUNCTIONS``
  function to calculate one simulation run and store its output; the call
  returns an ``EconomySimulationOutput`` naming the stored files.
- ``EconomyComparisonCall`` asks ``ECONOMY_COMPARISON_FUNCTION`` to compare two
  stored simulation outputs; the call returns a ``SingleYearMacroOutput``.

The entrypoint reads the region and dataset a run uses from the Public API's
region catalog and sends them as a ``CatalogPopulation``: the dataset's name,
and the household column and value that select the region's households. The
worker applies them itself, without policyengine.py's region registry.

A simulation's output columns do not depend on the report it is compared in,
so a run can be reused by any report, except for the labour supply response
columns: they are calculated only when ``labor_supply_response`` is set,
because calculating them in the UK runs the simulation again. The entrypoint
sets it when either policy of the report changes an elasticity
(``policy_affects_labor_supply``).
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Annotated, Literal, Self
from uuid import UUID

from pydantic import Field, JsonValue, field_validator, model_validator

from policyengine_simulation_contract.stage12_execution import (
    ArtifactReference,
    BundleProvenance,
    ContractText,
    CountryId,
    RowIdentity,
    Sha256Digest,
    StorageUri,
    StrictContractModel,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

ECONOMY_SIMULATION_FUNCTIONS: Mapping[str, str] = {
    "us": "run_economy_simulation_us",
    "uk": "run_economy_simulation_uk",
}
ECONOMY_COMPARISON_FUNCTION = "compare_economy_simulations"

# The parameters whose change makes policyengine.py calculate a labour supply
# response, by country: a parameter at or below one of these paths. They are
# policyengine.py's own (``policyengine.outputs.labor_supply_response``); the
# executor's tests check that the two agree.
LABOR_SUPPLY_RESPONSE_PARAMETER_PREFIXES: Mapping[str, tuple[str, ...]] = {
    "us": (
        "gov.simulation.labor_supply_responses.elasticities.income",
        "gov.simulation.labor_supply_responses.elasticities.substitution",
    ),
    "uk": (
        "gov.simulation.labour_supply_responses.income_elasticity",
        "gov.simulation.labour_supply_responses.substitution_elasticity",
        "gov.simulation.labor_supply_responses.income_elasticity",
        "gov.simulation.labor_supply_responses.substitution_elasticity",
    ),
}

# A parameter's values by period: a date (``2026-01-01``) or an inclusive
# ``start.stop`` pair of dates, as policyengine.py's policy dictionaries key
# them.
type PolicyChanges = dict[str, dict[str, JsonValue]]
FilterValue = Annotated[str, Field(min_length=1, max_length=4096)]
YEAR_RANGE = (1900, 2200)
_INTEGER = re.compile(r"-?\d+")


def policy_affects_labor_supply(country: str, policy: Mapping[str, object]) -> bool:
    """Return whether ``policy`` changes a labour supply elasticity."""
    prefixes = LABOR_SUPPLY_RESPONSE_PARAMETER_PREFIXES.get(country, ())
    return any(
        name == prefix or name.startswith(f"{prefix}.")
        for name in policy
        for prefix in prefixes
    )


def _household_scalar(text: str) -> str | int:
    """Return an integer written as one (US FIPS numbers and GEOIDs), else text."""
    return int(text) if _INTEGER.fullmatch(text) else text


class CatalogRowFilter(StrictContractModel):
    """The household column and value that select a region's households.

    ``value`` is the text the region catalog stores: one value, or a JSON list
    of text values when a region covers several (England, the nine English
    regions).
    """

    field: ContractText
    value: FilterValue

    @field_validator("value")
    @classmethod
    def _decodable(cls, value: str) -> str:
        if value.startswith("["):
            try:
                items = json.loads(value)
            except ValueError:
                msg = "a list filter value must be a JSON list of text values"
                raise ValueError(msg) from None
            if (
                not isinstance(items, list)
                or not items
                or not all(isinstance(item, str) and item for item in items)
            ):
                msg = "a list filter value must be a JSON list of text values"
                raise ValueError(msg)
        return value

    def household_value(self) -> str | int | list[str | int]:
        """Return the value or values a household's column is compared with.

        Each value that is written as an integer is compared as one, so a
        stored ``"6"`` selects the households whose ``state_fips`` is 6.
        """
        if self.value.startswith("["):
            return [_household_scalar(item) for item in json.loads(self.value)]
        return _household_scalar(self.value)


class CatalogPopulation(StrictContractModel):
    """The region and dataset one economy run uses, as its catalog version has them.

    ``dataset_name`` names a dataset of the policyengine.py release. A
    region's ``row_filter`` selects its households in that dataset; a
    national region has none. ``state_code`` names the US state a state or
    congressional-district region lies in. A US national run also carries
    ``state_filters``, each state region's filter by its code, so it can
    calculate its states in separate segments and add them up.
    """

    region_code: ContractText
    dataset_name: ContractText
    row_filter: CatalogRowFilter | None = None
    state_code: Annotated[str, Field(pattern=r"^[A-Z]{2}$")] | None = None
    state_filters: dict[str, CatalogRowFilter] | None = None

    @model_validator(mode="after")
    def _state_filters_for_a_national_region(self) -> Self:
        if self.state_filters is None:
            return self
        if self.row_filter is not None:
            msg = "only a national region, which has no filter, has state filters"
            raise ValueError(msg)
        if not self.state_filters or any(
            not code.startswith("state/") for code in self.state_filters
        ):
            msg = "state filters must be keyed by state region codes"
            raise ValueError(msg)
        return self


class EconomySimulationCall(StrictContractModel):
    """Calculate one economy simulation run, and store its output.

    ``policy`` holds the parameter values the run's policy, and its dynamic if
    any, set, by parameter name as the run's catalog version names them;
    current law has none. ``policyengine_version`` must be the version the
    called application installs. ``artifact_environment`` names the
    deployment's environment in the stored files' paths.
    """

    schema_version: Literal[1] = 1
    simulation_run_id: UUID
    country: CountryId
    year: Annotated[int, Field(ge=YEAR_RANGE[0], le=YEAR_RANGE[1])]
    policyengine_version: ContractText
    policy: PolicyChanges
    population: CatalogPopulation
    labor_supply_response: bool = False
    artifact_environment: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")]


class EconomySimulationOutput(StrictContractModel):
    """What an economy simulation call stored: its input and its output.

    ``output`` is the Parquet file of the simulation's entity tables, whose
    columns follow the output plan with digest ``output_plan_sha256``.
    """

    schema_version: Literal[1] = 1
    simulation_run_id: UUID
    input: ArtifactReference
    output: ArtifactReference
    output_plan_sha256: Sha256Digest
    row_identity: RowIdentity
    bundle: BundleProvenance


class EconomyComparisonSide(StrictContractModel):
    """One simulation run a comparison reads: its policy and stored output."""

    simulation_run_id: UUID
    policy: PolicyChanges
    output_uri: StorageUri
    output_sha256: Sha256Digest


class EconomyComparisonCall(StrictContractModel):
    """Compare two stored economy simulation outputs of one report run.

    Both simulation runs calculated the same region and dataset for the same
    catalog version and year; the call returns a ``SingleYearMacroOutput``.
    """

    schema_version: Literal[1] = 1
    report_run_id: UUID
    country: CountryId
    year: Annotated[int, Field(ge=YEAR_RANGE[0], le=YEAR_RANGE[1])]
    policyengine_version: ContractText
    region_code: ContractText
    dataset_name: ContractText
    baseline: EconomyComparisonSide
    reform: EconomyComparisonSide
