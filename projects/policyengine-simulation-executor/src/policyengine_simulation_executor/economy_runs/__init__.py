"""The v2 economy path's Modal calls: one per simulation run, one per comparison.

The Simulation Entrypoint starts these calls for its simulation runs and
report runs (``policyengine_simulation_contract.economy_runs``). They run on
the Stage 12 workers' machinery, with the region and dataset each run's
catalog version has.
"""
