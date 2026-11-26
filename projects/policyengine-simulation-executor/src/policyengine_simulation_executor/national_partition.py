"""Static region-group partition for segmented national runs.

A plain US national macro request runs as one simulation per group below
(each a ``region_group`` request over whole states), and a coordinator
reduces the groups' microdata back into the national output. Partitioning
by whole states with row-filter scoping preserves national weights, so the
reduce is exact — see the segmented-national plan.

Provenance: household-balanced 20-way partition computed from the actual
dataset household counts (LPT greedy over per-state rows; no state needed
splitting into districts). Groups hold 2,440-3,042 of the 57,240 sample
households each. Rebalance only with a measured partition; group count and
membership changes alter per-child runtimes, not correctness.
"""

from policyengine_simulation_executor.stage12_cache.partition import (
    US_REGION_GROUPS,
)

# Stage 12's partition is the one list of the 20 groups: the precomputed
# group baselines are built from it (``stage12_cache.levels_us``), so the
# segmented runs that read them must use the same groups.
US_NATIONAL_REGION_GROUPS: tuple[tuple[str, ...], ...] = US_REGION_GROUPS


def national_region_groups(country: str) -> list[list[str]] | None:
    """Return the region-group partition for a country's segmented national run.

    Returns None when the country has no partition (only the US has one),
    in which case national requests run monolithically.
    """
    if country.lower() == "us":
        return [list(group) for group in US_NATIONAL_REGION_GROUPS]
    return None
