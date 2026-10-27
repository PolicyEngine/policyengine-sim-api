"""Fixtures for gateway package import regression tests."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

GATEWAY_MODEL_MODULE = "policyengine_simulation_contract.gateway_models"
GATEWAY_ENDPOINTS_MODULE = "policyengine_simulation_gateway.endpoints"
GATEWAY_PACKAGE_MODULE = "policyengine_simulation_gateway"
FASTAPI_MODULE = "fastapi"

# endpoints.py re-exports the routes and their helpers from these modules.
GATEWAY_ROUTE_MODULES = tuple(
    f"{GATEWAY_PACKAGE_MODULE}.{module}"
    for module in (
        "bundle_payloads",
        "job_creation_routes",
        "job_status_routes",
        "modal_routing",
        "route_resolution",
        "routes_common",
        "version_routes",
    )
)

GATEWAY_MODEL_IMPORT_MODULES = (
    FASTAPI_MODULE,
    GATEWAY_PACKAGE_MODULE,
    GATEWAY_ENDPOINTS_MODULE,
    *GATEWAY_ROUTE_MODULES,
    GATEWAY_MODEL_MODULE,
)


@dataclass(frozen=True)
class GatewayImportModuleNames:
    """Module names involved in the gateway model import boundary."""

    endpoints: str = GATEWAY_ENDPOINTS_MODULE
    models: str = GATEWAY_MODEL_MODULE
    fastapi: str = FASTAPI_MODULE


@pytest.fixture
def gateway_import_module_names() -> GatewayImportModuleNames:
    """Return the module names the import-boundary tests assert on."""
    return GatewayImportModuleNames()


@pytest.fixture
def isolated_gateway_model_import_modules() -> Iterator[None]:
    """Temporarily clear modules that would mask import side effects."""
    previous_modules = {
        module_name: sys.modules.pop(module_name, None)
        for module_name in GATEWAY_MODEL_IMPORT_MODULES
    }

    try:
        yield
    finally:
        for module_name in GATEWAY_MODEL_IMPORT_MODULES:
            sys.modules.pop(module_name, None)
        sys.modules.update(
            {
                module_name: module
                for module_name, module in previous_modules.items()
                if module is not None
            }
        )
