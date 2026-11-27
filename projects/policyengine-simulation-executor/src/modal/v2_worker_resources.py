"""CPU, memory, time and container limits of the v2 app's simulation workers.

``src/modal/v2_app.py`` gives each simulation worker class, and the plain
function of the same name kept for callers on older code, these settings
together with its image and secrets.
"""

from __future__ import annotations

from typing import Any

from policyengine_simulation_executor.stage12_timeouts import (
    UK_SIMULATION_TIMEOUT_SECONDS,
    US_SIMULATION_TIMEOUT_SECONDS,
)

SEGMENT_WORKER_RESOURCES: dict[str, Any] = {
    "cpu": 8.0,
    "memory": 32768,
    "timeout": 3600,
    "retries": 0,
    # Every segment of every US simulation call that may run at once: a
    # segment queued behind this limit counts against its caller's wait.
    "max_containers": 1000,
}
US_SIMULATION_WORKER_RESOURCES: dict[str, Any] = {
    "cpu": 8.0,
    "memory": 32768,
    "timeout": US_SIMULATION_TIMEOUT_SECONDS,
    "retries": 0,
    # Both simulations of every coordinator call that may run at once. The
    # simulation runs and cliff calculations the simulation service starts
    # directly share the same containers.
    "max_containers": 50,
}
UK_SIMULATION_WORKER_RESOURCES: dict[str, Any] = {
    **US_SIMULATION_WORKER_RESOURCES,
    "timeout": UK_SIMULATION_TIMEOUT_SECONDS,
}
