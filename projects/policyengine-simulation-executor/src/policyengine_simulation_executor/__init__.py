"""PolicyEngine simulation executor: the economy-comparison API and its runtime."""

from fastapi import FastAPI
from policyengine_observability import ObservabilityRuntime

from .simulation import create_router

"""
Application defined as routers completely indipendent of environment allowing it
to easily be run in whatever cloud provider container or desktop or test environment.
"""


def initialize(app: FastAPI, runtime: ObservabilityRuntime) -> None:
    """Mount the simulation routes on ``app``, reporting telemetry via ``runtime``."""
    app.include_router(create_router(runtime))
