"""Which year files and baselines the v2 report app's cache builds, per country.

The level handlers (``levels``) say what each country and dataset level
builds; these settings say which of those levels the v2 cache covers. They
are the cache's own, separate from the run_simulation app's
``CountryPrebuilt.year_file_levels`` and ``baseline_levels``, so a task that
adds files or fixed baseline IDs to the run_simulation app does not add them
to the v2 cache by itself.

``CACHE_BASELINE_LEVELS`` also decides which runs get a fixed cache ID at run
time (``stage12_runtime.cached_simulation.qualifying_cache_identity``): only a
run whose level the cache precomputes baselines for. Today that is the US
state groups, the 20 groups a nationwide US report splits into. UK levels
join at task 2.2 · 6; US single states only if task 2.1 is extended to the v2
report app.

Every function reads the settings when called, so tests can replace them.
"""

from __future__ import annotations

from policyengine_simulation_executor.stage12_cache.levels import DatasetLevel

# The year-file levels the cache builds and the image holds, per country.
CACHE_YEAR_FILE_LEVELS: dict[str, tuple[DatasetLevel, ...]] = {
    "us": (DatasetLevel.NATIONAL, DatasetLevel.STATE),
}
# The levels whose current-law baselines the cache precomputes, per country.
CACHE_BASELINE_LEVELS: dict[str, tuple[DatasetLevel, ...]] = {
    "us": (DatasetLevel.STATE_GROUP,),
}


def cache_year_file_levels(country: str) -> tuple[DatasetLevel, ...]:
    """Return the year-file levels the cache builds for ``country``, if any."""
    return CACHE_YEAR_FILE_LEVELS.get(country.lower(), ())


def cache_baseline_levels(country: str) -> tuple[DatasetLevel, ...]:
    """Return the levels whose baselines the cache precomputes for ``country``."""
    return CACHE_BASELINE_LEVELS.get(country.lower(), ())
