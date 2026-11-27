"""Run one calculation before Modal takes the v2 US workers' memory snapshot.

A process's first calculation for a year does work that its later
calculations for that year reuse: mostly building the US parameter values at
the calculation date and the SPM threshold amounts, about 20 s of the first
calculation when profiled on Modal ``testing`` (2026-11-25, in the
run_simulation app). A later request can reuse only what the packages keep
for the whole process, such as the module-level parameter tree that every
simulation's baseline branch shares (``share_spm_policy`` in policyengine-us),
spm-calculator's threshold tables and the modules the calculation imports; a
request's reform builds its own parameter tree. The v2 workers' snapshot hook
otherwise only loads the models. So ``SimulationSegmentWorkerUS`` and
``SimulationWorkerUS`` in ``src/modal/v2_app.py`` run
:func:`warm_up_first_us_calculation` in their ``@modal.enter(snap=True)``
hook, after the preload, and every container restored from the snapshot
starts with that work done for the warm-up year.
The run_simulation app's workers run the same kind of warm-up, which made
restored containers' first calculation 11 to 16 s faster on Modal
``testing``, with identical results; this one hasn't been measured.

The warm-up calculates one segment as a segmented national run's child
receives it: the reform side of the first region group whose current-law
baseline the v2 cache precomputes, for the first precompute year, under a
small reform. A v2 segment calculates one simulation, so a reform segment
always calculates, and never reads or computes a baseline, whatever the
cache holds. It runs only where it is safe before a snapshot, and only where
Modal takes one:

- **Only in a US worker container Modal snapshots.** Modal runs the hook in
  every container that isn't restored, and takes a snapshot only where its
  worker set ``MODAL_ENABLE_SNAP_RESTORE`` to ``1``. Elsewhere the warm-up
  would delay the container's first call by about as long as a calculation.
  The UK worker's hook calls it too, and it does nothing there: the UK image
  holds no prebuilt UK year file, so a UK warm-up would build its dataset
  before the snapshot.
- **No connection and no Secret.** It runs only when the US models were
  preloaded, since importing them again could fetch the data release
  manifest again, and when the image's prebuilt folder holds the group's
  year file in a form .py reuses, looked up as a run looks it up, so nothing
  is downloaded or built from source. The calculation runs inside
  ``skip_gcp_credential_setup``, so the v2 calculation's Google Cloud
  credential setup reads no Modal Secret and writes no credentials file.
  (The preload before it already reads the Hugging Face token and fetches
  the data release manifest, as ``country_model_preload`` says.)
- **No telemetry.** It configures no observability and calculates with no
  runtime, so it writes no operation record or span, and the container's
  first call is still number 1 (``ContainerCalls`` in
  ``src/modal/container_calls.py``).
- **Nothing left for a request.** It removes the simulation it added to
  .py's in-process cache and the output file it saved in the prebuilt
  folder (``_removing_what_the_calculation_leaves``), so a later call finds
  the image's files as it would without the warm-up. What stays is what the
  packages keep for the whole process.
- **Never fatal.** A failure or skip logs a warning, and the container
  starts as it would without the warm-up. The executor modules it uses are
  looked up when it runs, not when ``v2_app.py`` imports this module, so a
  move or rename in them costs only the warm-up.

The run_simulation app's warm-up (``src/modal/snapshot_warm_up.py``) does the
same for that app's workers. This module shares no code with it, since that
app is to be removed while this one stays.
"""

from __future__ import annotations

import contextlib
import gc
import logging
import os
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

from src.modal.country_model_preload import COUNTRY_MODEL_MODULES

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from policyengine_simulation_contract.stage12_execution import (
        PlannedSimulationExecutionInput,
    )
    from policyengine_simulation_executor.stage12_runtime.segment_contracts import (
        Stage12SegmentInput,
    )

logger = logging.getLogger(__name__)

# Modal's worker sets this to "1" in the containers it takes a snapshot from
# or restores (``run_function`` in modal/_container_entrypoint.py, Modal
# 1.6.4). Restored containers don't run the snapshot hook.
SNAPSHOT_FLAG_ENV_NAME = "MODAL_ENABLE_SNAP_RESTORE"
WARM_UP_COUNTRY = "us"
# The reform makes the warm-up calculate a reform simulation, as a request
# does: the Child Tax Credit's base amount, raised to $2,500.
WARM_UP_REFORM_PARAMETER = "gov.irs.credits.ctc.amount.base[0].amount"
WARM_UP_REFORM_VALUE = 2500


class _WarmUpUnsafeError(Exception):
    """The warm-up can't run safely before the snapshot; the message says why."""


def warm_up_first_us_calculation(countries: Iterable[str]) -> None:
    """Run the warm-up calculation in a US worker container Modal snapshots.

    ``countries`` are the worker class's ``snapshot_countries``. Does nothing
    for a worker without the US, or in a container Modal takes no snapshot
    from. Nothing here may stop a container from starting: a failure or skip
    logs a warning, and each container's first calculation then does its
    one-time work itself, as before.
    """
    if WARM_UP_COUNTRY not in tuple(countries):
        return
    if os.environ.get(SNAPSHOT_FLAG_ENV_NAME) != "1":
        return
    try:
        _calculate_unless_unsafe()
    except _WarmUpUnsafeError as unsafe:
        logger.warning(
            "Warm-up calculation skipped, so each restored container's first "
            "calculation does its one-time work itself: %s",
            unsafe,
        )
    except Exception:  # noqa: BLE001  # the warm-up only makes first calls faster
        logger.warning(
            "Warm-up calculation failed; each restored container's first "
            "calculation does its one-time work itself",
            exc_info=True,
        )


def warm_up_segment_input() -> Stage12SegmentInput:
    """Return the warm-up's segment: a precomputed group's reform, first year.

    The group is the first whose current-law baseline the v2 cache
    precomputes for the country's first precompute year, which is also a
    segmented national run's first segment. Its simulation is the
    precompute's planned reform side for that group, given the warm-up's
    reform and planned again as a report run's reform child with that policy
    is planned.
    """
    from policyengine_simulation_contract.stage12_execution import (  # noqa: PLC0415  # imported here so a move of the planning code costs only the warm-up, never the app's import
        SimulationExecutionInput,
    )
    from policyengine_simulation_executor.stage12_cache import (  # noqa: PLC0415  # as above
        precompute_planning,
    )
    from policyengine_simulation_executor.stage12_cache.cache_levels import (  # noqa: PLC0415  # as above
        cache_baseline_levels,
    )
    from policyengine_simulation_executor.stage12_runtime import (  # noqa: PLC0415  # as above
        output_planning,
    )
    from policyengine_simulation_executor.stage12_runtime.segment_contracts import (  # noqa: PLC0415  # as above
        Stage12SegmentInput,
    )

    baseline_levels = cache_baseline_levels(WARM_UP_COUNTRY)
    if not baseline_levels:
        msg = f"the v2 cache precomputes no {WARM_UP_COUNTRY} baseline"
        raise _WarmUpUnsafeError(msg)
    prebuilt = precompute_planning.country_prebuilt(WARM_UP_COUNTRY)
    year = prebuilt.years[0]
    first_target = prebuilt.handlers[baseline_levels[0]].baseline_targets(year)[0]
    group = tuple(first_target.request["region_group"])
    template = precompute_planning.scoped_planned_simulation(year, group, reform=True)
    reform = SimulationExecutionInput.model_validate(
        {
            **template.model_dump(mode="json", exclude={"output_plan"}),
            "policy": {
                WARM_UP_REFORM_PARAMETER: {
                    f"{year}-01-01.2100-12-31": WARM_UP_REFORM_VALUE
                }
            },
        }
    )
    planned = output_planning.plan_simulation_input(
        reform, output_planning.resolve_simulation_output_plan(reform)
    )
    return Stage12SegmentInput(simulation=planned, segment_index=0, region_codes=group)


def _calculate_unless_unsafe() -> None:
    """Run the warm-up calculation; raise ``_WarmUpUnsafeError`` when it isn't safe."""
    not_preloaded = [
        module_name
        for module_name in COUNTRY_MODEL_MODULES[WARM_UP_COUNTRY]
        if module_name not in sys.modules
    ]
    if not_preloaded:
        msg = f"the preload didn't import {', '.join(not_preloaded)}"
        raise _WarmUpUnsafeError(msg)
    from policyengine_simulation_executor import (  # noqa: PLC0415  # resolved per call so tests can replace calculate_segment; loaded in the worker container, not at deploy time
        stage12_runtime,
    )
    from policyengine_simulation_executor.gcp_credentials import (  # noqa: PLC0415  # as above
        skip_gcp_credential_setup,
    )

    segment = warm_up_segment_input()
    prebuilt_folder = _prebuilt_folder_with_year_file(segment)
    started = time.monotonic()
    with (
        skip_gcp_credential_setup(),
        _removing_what_the_calculation_leaves(prebuilt_folder),
    ):
        # Sent as the fan-out sends a segment to its worker.
        stage12_runtime.calculate_segment(
            segment.model_dump(mode="python"), runtime=None
        )
    logger.info(
        "Warm-up calculation of the reform for region group %s in %s finished in %.1fs",
        "+".join(segment.region_codes),
        segment.simulation.year,
        time.monotonic() - started,
    )


def _scoped_simulation(segment: Stage12SegmentInput) -> PlannedSimulationExecutionInput:
    """Return the segment's simulation as ``calculate_segment`` scopes it."""
    options = {
        key: value
        for key, value in segment.simulation.options.items()
        if key != "segmented"
    }
    options["region_group"] = list(segment.region_codes)
    return segment.simulation.model_copy(update={"options": options})


def _prebuilt_folder_with_year_file(segment: Stage12SegmentInput) -> Path:
    """Return the prebuilt folder holding the segment's year file.

    Looked up as ``calculate_simulation_frames`` looks it up for the scoped
    segment: the region and dataset resolution, then the image's
    prebuilt-folder lookup, which finds no folder for a year file the image
    lacks or .py wouldn't reuse. Raises ``_WarmUpUnsafeError`` then: the
    calculation would build the year file from its source, which it may
    download.
    """
    from policyengine_simulation_executor.simulation_runtime import (  # noqa: PLC0415  # resolved per call so tests can patch simulation_runtime
        _country_module,
        _prebuilt_folder,
        _resolve_dataset_selection,
        resolve_region,
    )
    from policyengine_simulation_executor.stage12_runtime.local_area_datasets import (  # noqa: PLC0415  # see warm_up_segment_input
        dataset_region,
        local_area_dataset_name,
    )
    from policyengine_simulation_executor.stage12_runtime.worker_inputs import (  # noqa: PLC0415  # as above
        simulation_runtime_params,
    )

    simulation = _scoped_simulation(segment)
    country = simulation.geography.country
    params = simulation_runtime_params(simulation)
    region = resolve_region(
        country_module=_country_module(country), country=country, params=params
    )
    selection = _resolve_dataset_selection(
        params,
        region_resolution=dataset_region(region, local_area_dataset_name(simulation)),
    )
    prebuilt = _prebuilt_folder(country, region, selection, simulation.year)
    if prebuilt is None:
        msg = (
            "the image holds no prebuilt year file .py would reuse for region "
            f"group {'+'.join(segment.region_codes)} in {simulation.year}"
        )
        raise _WarmUpUnsafeError(msg)
    return Path(prebuilt.folder)


@contextlib.contextmanager
def _removing_what_the_calculation_leaves(output_folder: Path) -> Iterator[None]:
    """Remove the simulations and output files a calculation adds, even if it fails.

    .py's ``Simulation.ensure()`` caches the reform in the process under a new
    random ID and saves its output beside the year file it loaded, in
    ``output_folder``, as ``<storage ID>.h5``. Left there, both would go into
    the snapshot, taking memory and disk in every restored container.
    """
    from policyengine.core.simulation import (  # noqa: PLC0415  # resolved per call so tests can patch policyengine's simulation cache
        _cache as simulation_cache,
    )

    cached_before = set(simulation_cache._cache)  # noqa: SLF001  # policyengine's LRUCache has no public listing or remove
    outputs_before = set(output_folder.glob("*.h5"))
    try:
        yield
    finally:
        for storage_id in [
            storage_id
            for storage_id in simulation_cache._cache  # noqa: SLF001  # as above
            if storage_id not in cached_before
        ]:
            simulation_cache._cache.pop(storage_id, None)  # noqa: SLF001  # as above
        for output in set(output_folder.glob("*.h5")) - outputs_before:
            output.unlink(missing_ok=True)
        gc.collect()
