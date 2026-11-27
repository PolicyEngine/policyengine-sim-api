"""Run one calculation before Modal takes the simulation workers' memory snapshot.

A process's first calculation for a year does work that its later
calculations for that year reuse. Profiled on Modal ``testing`` on
2026-11-25, that was about 20 s of the first calculation, mostly building the
US parameter values at the calculation date (``ParameterNode._get_at_instant``)
and the SPM threshold amounts; the snapshot hook's model preload does neither.
That profile compared two calculations in one process, so it doesn't show
which of this work a warm-up under another group and reform leaves for a
later request. A request can reuse only what the packages keep for the whole
process, such as the module-level parameter tree that every simulation's
baseline branch shares (``share_spm_policy`` in policyengine-us),
spm-calculator's threshold tables and price index series, and the modules the
calculation imports. A request's reform builds its own parameter tree, and
each simulation its own SPM amount cache.

So ``SimulationWorker`` and ``SimulationSegmentWorker`` in
``src/modal/app.py`` run :func:`warm_up_first_calculation` in their
``@modal.enter(snap=True)`` hook, after the preload, and every container
restored from the snapshot starts with that work done for the warm-up year.
Measured on Modal ``testing`` (2026-11-25 on 8 CPUs, 2026-11-26 on 1 CPU), a
warm-up of another region group under another reform made restored
containers' first calculation 11 to 16 s faster, with identical results, and
the containers Modal took snapshots from started about 70 to 100 s later.

The warm-up calculates one segmented national run's child request: group 1
of the US group-of-states handler, whose year file and current-law baseline
the precompute builds into the image, for the first prebuilt year, under a
small reform. It runs only where it is safe before a snapshot, and only where
Modal takes one:

- **Only in a container Modal snapshots.** Modal runs the hook in every
  container that isn't restored, and takes a snapshot only where its worker
  set ``MODAL_ENABLE_SNAP_RESTORE`` to ``1``. Elsewhere the warm-up would
  delay the container's first call by about as long as a calculation, to
  save that call about 15 s.
- **No connection.** It runs only when the US models were preloaded, since
  importing them again could fetch the data release manifest again, and
  when the image's prebuilt folder holds the group's year file, in a form
  .py reuses, and its baseline. Both are looked up as a run looks them up,
  so nothing is downloaded or built from source. It skips the Google Cloud
  credential setup of ``run_simulation_impl``, which reads a Modal Secret,
  and reads no Secret itself. (The preload before it already reads the
  Hugging Face token and fetches the data release manifest, as
  ``country_model_preload`` says.)
- **No telemetry.** It configures no observability and gives the
  calculation a runtime that records and sends nothing, so it writes no
  operation record or span and no call number (``ContainerCalls`` in
  ``src/modal/container_calls.py``).
- **Nothing left for a request.** It removes the simulations it added to
  .py's in-process cache and the output files it saved in the prebuilt
  folder, so a later call loads its baseline from the image as it would
  without the warm-up. What stays is what the packages keep for the whole process, as
  above. One case differs: when the baked baseline is incomplete, out of
  date or unreadable, the calculation runs it again and saves it over the
  image's file, as a call would. The warm-up keeps that copy, which holds
  the baseline the calculation needed, so it goes into the snapshot and
  restored containers load it; it logs a warning naming the file, since
  those containers' calls then don't report the baked file's problem.
- **Never fatal.** A failure or skip logs a warning, and the container
  starts as it would without the warm-up. The executor modules it uses are
  imported when it runs, not when ``app.py`` imports this module, so a move
  or rename in them costs only the warm-up.
"""

from __future__ import annotations

import contextlib
import gc
import logging
import os
import sys
import time
from contextlib import nullcontext
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from src.modal.country_model_preload import COUNTRY_MODEL_MODULES

if TYPE_CHECKING:
    from collections.abc import Iterator

    from policyengine_observability import ObservabilityRuntime
    from policyengine_simulation_executor.stage12_cache.levels import BaselineTarget

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


class _RecordNothingRuntime:
    """The observability runtime the warm-up's calculation reports to.

    It records and sends nothing. A real runtime configured before the
    snapshot would carry one process identity into every restored container,
    and would export the warm-up as if it were a call.
    """

    def span(self, *_args: object, **_kwargs: object) -> nullcontext[None]:
        """Open nothing."""
        return nullcontext()

    def operation(self, *_args: object, **_kwargs: object) -> nullcontext[None]:
        """Open nothing."""
        return nullcontext()

    def set_context(self, **_attributes: object) -> None:
        """Keep nothing."""

    def event(self, *_args: object, **_kwargs: object) -> None:
        """Record nothing."""

    def record_exception(self, *_args: object, **_kwargs: object) -> None:
        """Record nothing."""

    def capture_context(self) -> dict[str, str]:
        """Return an empty context."""
        return {}


def warm_up_target() -> BaselineTarget:
    """Return group 1's precomputed current-law baseline, for the first prebuilt year.

    The US settings' group-of-states handler names the groups and years the
    precompute bakes a current-law baseline for. Group 1 is the group the
    measurements on Modal ``testing`` warmed up with.
    """
    from policyengine_simulation_executor.precompute_planning import (  # noqa: PLC0415  # imported here so a move of the precompute code costs only the warm-up, never the app's import
        country_prebuilt,
    )
    from policyengine_simulation_executor.stage12_cache.levels import (  # noqa: PLC0415  # as above
        DatasetLevel,
    )

    prebuilt = country_prebuilt(WARM_UP_COUNTRY)
    group_handler = prebuilt.handlers[DatasetLevel.STATE_GROUP]
    return group_handler.baseline_targets(prebuilt.years[0])[0]


def warm_up_request(target: BaselineTarget) -> dict[str, Any]:
    """Return the warm-up's request: ``target``'s request, with a reform added.

    ``target.request`` is the request a segmented national run's child for the
    group receives under current law.
    """
    request = dict(target.request)
    request["reform"] = {
        WARM_UP_REFORM_PARAMETER: {
            f"{target.year}-01-01.2100-12-31": WARM_UP_REFORM_VALUE
        }
    }
    request["_emit_microdata"] = True
    return request


def warm_up_first_calculation() -> None:
    """Run the warm-up calculation in a container Modal takes a snapshot from.

    Does nothing in any other container. Nothing here may stop a container
    from starting: a failure or skip logs a warning, and each container's
    first calculation then does its one-time work itself, as before.
    """
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
    from policyengine_simulation_executor.simulation_runtime import (  # noqa: PLC0415  # resolved per call so tests can patch simulation_runtime; loaded in the worker container, not at deploy time
        _run_simulation_impl_core,
    )

    target = warm_up_target()
    request = warm_up_request(target)
    prebuilt_folder = _prebuilt_folder_with_baked_inputs(target, request)
    started = time.monotonic()
    with _removing_what_the_calculation_leaves(prebuilt_folder):
        _run_simulation_impl_core(
            request, runtime=cast("ObservabilityRuntime", _RecordNothingRuntime())
        )
    logger.info(
        "Warm-up calculation of the %s for %s finished in %.1fs",
        target.label,
        target.year,
        time.monotonic() - started,
    )


def _prebuilt_folder_with_baked_inputs(
    target: BaselineTarget, request: dict[str, Any]
) -> Path:
    """Return the prebuilt folder holding the warm-up's year file and baseline.

    Both are looked up as a run of ``request`` looks them up: the region and
    dataset resolution, then the image's prebuilt-folder lookup, which finds
    no folder for a year file the image lacks or .py wouldn't reuse; and the
    precompute's identity for the group's baseline, which the image keeps
    beside the year file as ``<storage ID>.h5``. Raises ``_WarmUpUnsafeError``
    when either is missing: the calculation would then build the year file
    from its source, which it may download, or calculate the baseline.
    """
    from policyengine_simulation_executor.precompute_planning import (  # noqa: PLC0415  # resolved per call so tests can patch precompute_planning; and see warm_up_target
        baseline_target_identity,
    )
    from policyengine_simulation_executor.simulation_runtime import (  # noqa: PLC0415  # resolved per call so tests can patch simulation_runtime
        _country_module,
        _prebuilt_folder,
        _resolve_dataset_selection,
        resolve_region,
    )

    region = resolve_region(
        country_module=_country_module(target.country),
        country=target.country,
        params=request,
    )
    dataset = _resolve_dataset_selection(request, region_resolution=region)
    prebuilt = _prebuilt_folder(target.country, region, dataset, target.year)
    if prebuilt is None:
        msg = (
            f"the image holds no prebuilt year file .py would reuse for the "
            f"{target.label} in {target.year}"
        )
        raise _WarmUpUnsafeError(msg)
    identity = baseline_target_identity(target)
    if identity is None:
        msg = f"the {target.label} baseline for {target.year} gets no fixed ID"
        raise _WarmUpUnsafeError(msg)
    folder = Path(prebuilt.folder)
    baseline_filename = f"{identity.storage_id}.h5"
    if not (folder / baseline_filename).is_file():
        msg = (
            f"the prebuilt folder {folder} lacks the {target.label} baseline "
            f"{baseline_filename}"
        )
        raise _WarmUpUnsafeError(msg)
    return folder


@contextlib.contextmanager
def _removing_what_the_calculation_leaves(output_folder: Path) -> Iterator[None]:
    """Remove the simulations and output files a calculation adds, even if it fails.

    .py's ``Simulation.ensure()`` caches each simulation in the process (its
    baseline under the baked baseline's storage ID, its reform under a new
    random ID) and saves an output it calculated beside the year file it
    loaded, in ``output_folder``, as ``<storage ID>.h5``. Left there, they
    would go into the snapshot, and a later call for the warm-up's group
    would use the cached baseline instead
    of loading the baked one. An output saved over a file the image already
    held, when the baked baseline was incomplete, out of date or unreadable,
    is kept, and logged as a warning.
    """
    from policyengine.core.simulation import (  # noqa: PLC0415  # resolved per call so tests can patch policyengine's simulation cache
        _cache as simulation_cache,
    )

    cached_before = set(simulation_cache._cache)  # noqa: SLF001  # policyengine's LRUCache has no public listing or remove
    outputs_before = {
        output: _size_and_modification_time(output)
        for output in output_folder.glob("*.h5")
    }
    try:
        yield
    finally:
        for storage_id in [
            storage_id
            for storage_id in simulation_cache._cache  # noqa: SLF001  # as above
            if storage_id not in cached_before
        ]:
            simulation_cache._cache.pop(storage_id, None)  # noqa: SLF001  # as above
        for output in set(output_folder.glob("*.h5")) - outputs_before.keys():
            output.unlink(missing_ok=True)
        gc.collect()
        saved_over = sorted(
            output.name
            for output, state_before in outputs_before.items()
            if _size_and_modification_time(output) != state_before
        )
        if saved_over:
            logger.warning(
                "The warm-up calculation saved over %s in %s, because the baked "
                "file was incomplete, out of date or unreadable; containers "
                "restored from this snapshot start with the file as the warm-up "
                "left it",
                ", ".join(saved_over),
                output_folder,
            )


def _size_and_modification_time(path: Path) -> tuple[int, int] | None:
    """Return a file's size and modification time in nanoseconds, or None if gone."""
    try:
        status = path.stat()
    except FileNotFoundError:
        return None
    return status.st_size, status.st_mtime_ns
