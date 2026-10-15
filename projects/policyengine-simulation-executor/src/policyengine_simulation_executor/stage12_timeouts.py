"""Modal timeouts and waits shared by the Stage 12 app and report coordinator.

This module has no imports, so the Modal app declaration can read it without
loading the Stage 12 runtime.
"""

# Modal timeouts of the simulation workers that the report coordinator starts.
# A US worker starts 20 region-group calls (3,600 s each), then merges their
# results and writes one artifact.
US_SIMULATION_TIMEOUT_SECONDS = 3_900
UK_SIMULATION_TIMEOUT_SECONDS = 3_000

# One child-wait window opens when `coordinate_report` starts. It covers
# coordinator setup (claiming, output planning including the country model
# load, child-state creation, retained-artifact reads and dispatch) and the
# concurrent wait for both simulations, so setup time shortens the children's
# window instead of the time left after it. The window equals the longest
# simulation worker timeout, so the coordinator only stops waiting for a worker
# early by its own setup time and the time the worker spent queued in Modal.
# The coordinator's last poll never sleeps past the end of the window.
SIMULATION_WAIT_TIMEOUT_SECONDS = max(
    US_SIMULATION_TIMEOUT_SECONDS,
    UK_SIMULATION_TIMEOUT_SECONDS,
)

# An automatic (shadow) report then waits up to 15 minutes for the production
# result it compares against.
PRODUCTION_RESULT_WAIT_TIMEOUT_SECONDS = 900

# Time left after both waits to read and validate both simulation artifacts,
# aggregate, write the aggregate and comparison artifacts, and record the
# outcome or failure. A complete testing-environment US report, including all
# 40 region-group calculations, took 288 s.
REPORT_FINISHING_SECONDS = 600

# 3,900 s + 900 s + 600 s = 5,400 s.
COORDINATOR_TIMEOUT_SECONDS = (
    SIMULATION_WAIT_TIMEOUT_SECONDS
    + PRODUCTION_RESULT_WAIT_TIMEOUT_SECONDS
    + REPORT_FINISHING_SECONDS
)
