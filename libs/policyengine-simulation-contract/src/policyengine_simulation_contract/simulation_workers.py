"""Names of the Modal workers that run annual economy simulations.

Each versioned simulation app defines its two workers as Modal classes, so
their containers can restore from memory snapshots with the country models
already loaded:

- ``SimulationWorker.run_simulation`` runs one annual simulation. The gateway
  spawns it for every comparison request, and a budget-window batch spawns it
  once per year.
- ``SimulationSegmentWorker.run_simulation_segment`` runs one region group of
  a segmented national run. It is a separate class so the segment children
  draw from their own container pool.

The gateway and the executor both look the workers up by these names in a
deployed app, so both read them from here.

Apps deployed before the workers were classes define ``run_simulation`` as a
plain Modal function. The gateway still routes to those apps, for pinned
versions and after a rollback, so it falls back to that function name when an
app has no ``SimulationWorker`` class.
"""

SIMULATION_WORKER_CLASS = "SimulationWorker"
SIMULATION_WORKER_METHOD = "run_simulation"

SEGMENT_WORKER_CLASS = "SimulationSegmentWorker"
SEGMENT_WORKER_METHOD = "run_simulation_segment"

FUNCTION_FORM_SIMULATION_WORKER = "run_simulation"
