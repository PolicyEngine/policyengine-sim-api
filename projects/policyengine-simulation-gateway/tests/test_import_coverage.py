"""Import coverage for the packages installed in the gateway image."""

import importlib
import pkgutil

import policyengine_observability
import policyengine_simulation_gateway
from policyengine_simulation_gateway.testing import create_gateway_app


def test_every_gateway_module_imports():
    for module in pkgutil.walk_packages(
        policyengine_simulation_gateway.__path__,
        prefix="policyengine_simulation_gateway.",
    ):
        importlib.import_module(module.name)


def test_asgi_factory_builds_with_observability_v2():
    assert callable(policyengine_observability.configure)
    app = create_gateway_app()
    assert app.routes
