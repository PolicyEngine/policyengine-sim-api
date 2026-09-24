from fastapi import FastAPI
from policyengine_observability import ObservabilityRuntime
from .simulation import create_router

"""
Application defined as routers completely indipendent of environment allowing it
to easily be run in whatever cloud provider container or desktop or test environment.
"""


def initialize(app: FastAPI, runtime: ObservabilityRuntime):
    """
    attach all routes to the app and configure them to use the provided SQLModel engine
    and jwt settings.
    """
    app.include_router(create_router(runtime))
