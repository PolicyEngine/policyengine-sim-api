"""Run one calculation before Modal takes the simulation workers' memory snapshot.

A process's first calculation for a year builds work that its later
calculations for that year reuse: the US parameter tree's values at the
calculation date (``ParameterNode._get_at_instant``) and the SPM threshold
amounts. Profiled on Modal ``testing`` on 2026-11-25, that took about 20 s of
the first calculation, and the snapshot hook's model preload doesn't do it.
So ``SimulationWorker`` and ``SimulationSegmentWorker`` in
``src/modal/app.py`` run :func:`warm_up_first_calculation` in their
``@modal.enter(snap=True)`` hook, after the preload, and every container
restored from the snapshot starts with that work done for the warm-up year.
In the same measurement, a warm-up of one region group made restored
containers' first calculation 11 to 15 s faster, with identical results, and the
containers Modal took snapshots from started 72 to 97 s later.

The warm-up calculates one segmented national run's child request: a region
group whose dataset and current-law baseline the precompute bakes into the
image, for the first precompute year, under a small reform. It runs only
where it is safe before a snapshot, and only where Modal takes one:

- **Only in a container Modal snapshots.** Modal runs the hook in every
  container that isn't restored, and takes a snapshot only where its worker
  set ``MODAL_ENABLE_SNAP_RESTORE`` to ``1``. Elsewhere the warm-up would
  delay the container's first call by about as long as a calculation, to
  save that call about 15 s.
- **No connection.** It runs only when the US models were preloaded, since
  importing them again could fetch the data release manifest again, and
  when the group's year file and baseline are in the image's data folder,
  so nothing is downloaded or built from source. It skips the Google Cloud
  credential setup of ``run_simulation_impl``, which reads a Modal Secret:
  Secrets aren't read before the snapshot.
- **No telemetry.** It configures no observability and gives the
  calculation a runtime that records and sends nothing, so it writes no
  operation record or span and no call number (``_ContainerCalls``).
- **Nothing left for a request.** It removes the simulations it added to
  .py's in-process cache and the output files it saved in the data folder,
  so a later call loads its baseline from the image as it would without the
  warm-up. Only what .py and policyengine-core keep for every calculation
  stays: the values built for the warm-up year.
- **Never fatal.** A failure or skip logs a warning, and the container
  starts as it would without the warm-up.
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

from policyengine_simulation_executor.national_partition import (
    US_NATIONAL_REGION_GROUPS,
)
from policyengine_simulation_executor.precompute_planning import (
    PRECOMPUTE_COUNTRY,
    PRECOMPUTE_YEARS,
    cohort_params,
)
from src.modal.country_model_preload import COUNTRY_MODEL_MODULES

if TYPE_CHECKING:
    from collections.abc import Iterator

    from policyengine_observability import ObservabilityRuntime

logger = logging.getLogger(__name__)

# Modal's worker sets this to "1" in the containers it takes a snapshot from
# or restores (``run_function`` in modal/_container_entrypoint.py, Modal
# 1.6.4). Restored containers don't run the snapshot hook.
SNAPSHOT_FLAG_ENV_NAME = "MODAL_ENABLE_SNAP_RESTORE"
WARM_UP_YEAR = PRECOMPUTE_YEARS[0]
# The group the measurement on Modal ``testing`` warmed up with.
WARM_UP_REGION_GROUP = US_NATIONAL_REGION_GROUPS[0]
# The reform makes the warm-up calculate a reform simulation, as a request
# does: the Child Tax Credit's base amount, raised to $2,500.
WARM_UP_REFORM_PARAMETER = "gov.irs.credits.ctc.amount.base[0].amount"
WARM_UP_REFORM_VALUE = 2500


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


def warm_up_request() -> dict[str, Any]:
    """Return the warm-up's request: one region group's child of a national run."""
    request = cohort_params(WARM_UP_YEAR, list(WARM_UP_REGION_GROUP))
    request["reform"] = {
        WARM_UP_REFORM_PARAMETER: {
            f"{WARM_UP_YEAR}-01-01.2100-12-31": WARM_UP_REFORM_VALUE
        }
    }
    request["_emit_microdata"] = True
    return request


def warm_up_first_calculation() -> None:
    """Run the warm-up calculation in a container Modal takes a snapshot from.

    Does nothing in any other container. Nothing here may stop a container
    from starting: a failure or skip logs a warning, and each container's
    first calculation then builds its year's values itself, as before.
    """
    if os.environ.get(SNAPSHOT_FLAG_ENV_NAME) != "1":
        return
    started = time.monotonic()
    try:
        skip_reason = _calculate_unless_unsafe()
    except Exception:  # noqa: BLE001  # the warm-up only makes first calls faster
        logger.warning(
            "Warm-up calculation failed; each restored container's first "
            "calculation builds its year's values itself",
            exc_info=True,
        )
        return
    if skip_reason is not None:
        logger.warning(
            "Warm-up calculation skipped, so each restored container's first "
            "calculation builds its year's values itself: %s",
            skip_reason,
        )
        return
    logger.info(
        "Warm-up calculation of %s for %s finished in %.1fs",
        "+".join(WARM_UP_REGION_GROUP),
        WARM_UP_YEAR,
        time.monotonic() - started,
    )


def _calculate_unless_unsafe() -> str | None:
    """Run the warm-up calculation, or return why it would be unsafe to."""
    not_preloaded = [
        module_name
        for module_name in COUNTRY_MODEL_MODULES[PRECOMPUTE_COUNTRY]
        if module_name not in sys.modules
    ]
    if not_preloaded:
        return f"the preload didn't import {', '.join(not_preloaded)}"
    from policyengine_simulation_executor.simulation_runtime import (  # noqa: PLC0415  # resolved per call so tests can patch simulation_runtime; loaded in the worker container, not at deploy time
        _run_simulation_impl_core,
        resolve_data_folder,
    )

    data_folder = Path(resolve_data_folder())
    missing = _missing_baked_inputs(data_folder)
    if missing:
        return f"the image's data folder lacks {', '.join(missing)}"
    with _removing_what_the_calculation_leaves(data_folder):
        _run_simulation_impl_core(
            warm_up_request(),
            runtime=cast("ObservabilityRuntime", _RecordNothingRuntime()),
        )
    return None


def _missing_baked_inputs(data_folder: Path) -> list[str]:
    """Return the warm-up's baked files that aren't in ``data_folder``.

    The year file and the current-law baseline are named as the precompute
    names them, and as the calculation looks for them.
    """
    from policyengine_simulation_executor.artifact_keys import (  # noqa: PLC0415  # loaded in the worker container, not at deploy time
        baseline_artifact_filename,
    )
    from policyengine_simulation_executor.precompute_planning import (  # noqa: PLC0415  # resolved per call so tests can patch precompute_planning.cohort_identity
        cohort_identity,
    )

    identity = cohort_identity(WARM_UP_YEAR, list(WARM_UP_REGION_GROUP))
    filenames = (
        identity.dataset.filename,
        baseline_artifact_filename(identity.storage_id),
    )
    return [
        filename for filename in filenames if not (data_folder / filename).is_file()
    ]


@contextlib.contextmanager
def _removing_what_the_calculation_leaves(data_folder: Path) -> Iterator[None]:
    """Remove the simulations and output files a calculation adds, even if it fails.

    .py's ``Simulation.ensure()`` caches each simulation in the process (its
    baseline under the baked baseline's storage ID, its reform under a new
    random ID) and saves an output it calculated beside the dataset, as
    ``<storage ID>.h5``. Left there, they would go into the snapshot, and a
    later call for the warm-up's group would use the cached baseline instead
    of loading the baked one.
    """
    from policyengine.core.simulation import (  # noqa: PLC0415  # resolved per call so tests can patch policyengine's simulation cache
        _cache as simulation_cache,
    )

    cached_before = set(simulation_cache._cache)  # noqa: SLF001  # policyengine's LRUCache has no public listing or remove
    outputs_before = set(data_folder.glob("*.h5"))
    try:
        yield
    finally:
        for storage_id in [
            storage_id
            for storage_id in simulation_cache._cache  # noqa: SLF001  # as above
            if storage_id not in cached_before
        ]:
            simulation_cache._cache.pop(storage_id, None)  # noqa: SLF001  # as above
        for output in set(data_folder.glob("*.h5")) - outputs_before:
            output.unlink(missing_ok=True)
        gc.collect()
