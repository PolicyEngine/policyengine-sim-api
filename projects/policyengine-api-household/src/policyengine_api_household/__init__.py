from fastapi import FastAPI

from .routers import calculate

"""
Application defined as routers completely independent of environment allowing it
to easily be run in whatever cloud provider container or desktop or test environment.
"""


def initialize(app: FastAPI):
    """Attach all routes to the app."""
    app.include_router(calculate.router)
