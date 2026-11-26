"""Stage 12-owned US partition for segmented national calculations."""

from __future__ import annotations

from typing import TYPE_CHECKING

# The 20 groups live in stage12_cache, whose package imports no database
# code, so the run_simulation app's images can load them too.
from policyengine_simulation_executor.stage12_cache.partition import (
    US_REGION_GROUPS,
)

if TYPE_CHECKING:
    from policyengine_simulation_contract.stage12_execution import (
        PlannedSimulationExecutionInput,
    )

# Every national partition has this many region groups.
NATIONAL_PARTITION_GROUP_COUNT = 20


def stage12_region_groups(country: str) -> list[list[str]] | None:
    """Return a mutable copy of the Stage 12 partition when supported."""
    if country.lower() != "us":
        return None
    return [list(group) for group in US_REGION_GROUPS]


def stage12_region_groups_for_model(country: str, model: object) -> list[list[str]]:
    """Return the partition, retaining newly registered state regions.

    The static partition is measured for the certified dataset. A later model
    can register another state-level region before the partition is rebalanced;
    assigning it to the final group prevents silently omitting its households.
    """
    groups = stage12_region_groups(country)
    if groups is None:
        msg = f"Stage 12 has no national partition for {country!r}"
        raise ValueError(msg)
    registry = getattr(model, "region_registry", None)
    if registry is None:
        return groups
    registered = {
        region.code
        for region in registry.regions
        if getattr(region, "region_type", None) == "state"
    }
    covered = {code for group in groups for code in group}
    groups[-1].extend(sorted(registered - covered))
    return groups


def should_segment_simulation(simulation: PlannedSimulationExecutionInput) -> bool:
    """Return whether this logical simulation should use the US partition."""
    if simulation.output_plan.requirements.include_cliff_impacts:
        return False
    return should_segment_cliff_calculation(simulation)


def should_segment_cliff_calculation(
    simulation: PlannedSimulationExecutionInput,
) -> bool:
    """Return whether a cliff calculation of this simulation uses the US partition.

    It is split exactly when the simulation's own run is: a US national
    simulation, not marked ``segmented: false``, whose policy turns no
    labour-supply response on. Its cliff columns don't prevent it, since
    each person's depend only on their own household's calculation and the
    cliff share is taken once from the merged rows (``cliff_results``).
    """
    if simulation.geography.country != "us":
        return False
    if simulation.geography.region.strip().lower() != "us":
        return False
    if simulation.options.get("segmented") is False:
        return False
    if simulation.output_plan.requirements.labor_supply_response_active:
        return False
    return stage12_region_groups("us") is not None
