"""Names of the Modal classes that hold the v2 runtime's simulation workers.

The v2 application (``src/modal/v2_app.py`` in the executor) defines each
simulation worker as a method of a Modal class, so its containers restore from
a memory snapshot with the country's models already loaded:

- ``SimulationWorkerUS.run_single_simulation_us`` and
  ``SimulationWorkerUK.run_single_simulation_uk`` calculate one simulation.
  The report coordinator spawns them for a report's simulations, and the entry
  service for standalone simulation runs and cliff calculations.
- ``SimulationSegmentWorkerUS.run_single_simulation_segment_us`` calculates
  one region group of a segmented US simulation, in its own container pool.

Each method has the name the worker had as a plain Modal function. That name
is stored data: the v2 version manifest's ``single_simulation_callable``, the
coordinator's context and the comparison-run rows all hold it. So callers keep
the stored name and look its class up here.

Apps deployed before the classes define the workers only as plain functions
with these names, and the v2 application keeps those functions for callers
still running older code.
"""

from __future__ import annotations

from types import MappingProxyType

SEGMENT_WORKER_CLASS = "SimulationSegmentWorkerUS"
SEGMENT_WORKER_METHOD = "run_single_simulation_segment_us"

_WORKER_CLASS_BY_METHOD = MappingProxyType(
    {
        "run_single_simulation_us": "SimulationWorkerUS",
        "run_single_simulation_uk": "SimulationWorkerUK",
        SEGMENT_WORKER_METHOD: SEGMENT_WORKER_CLASS,
    }
)


def worker_class_name(method_name: str) -> str | None:
    """Return the Modal class whose method ``method_name`` is, or None.

    None means the v2 application defines ``method_name`` only as a plain
    function, as it does its coordinator and household functions.
    """
    return _WORKER_CLASS_BY_METHOD.get(method_name)
