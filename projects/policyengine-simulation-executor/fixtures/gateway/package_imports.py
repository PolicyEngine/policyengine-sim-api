"""Fixtures for gateway package import regression tests."""

from __future__ import annotations

import importlib
import sys
from collections.abc import Iterator
from dataclasses import dataclass

import pytest


GATEWAY_MODEL_MODULE = "src.modal.gateway.models"
GATEWAY_ENDPOINTS_MODULE = "src.modal.gateway.endpoints"
GATEWAY_PACKAGE_MODULE = "src.modal.gateway"
FASTAPI_MODULE = "fastapi"

GATEWAY_MODEL_IMPORT_MODULES = (
    FASTAPI_MODULE,
    GATEWAY_PACKAGE_MODULE,
    GATEWAY_ENDPOINTS_MODULE,
    GATEWAY_MODEL_MODULE,
)


@dataclass(frozen=True)
class GatewayImportModuleNames:
    """Module names involved in the gateway model import boundary."""

    endpoints: str = GATEWAY_ENDPOINTS_MODULE
    fastapi: str = FASTAPI_MODULE


@pytest.fixture()
def gateway_import_module_names() -> GatewayImportModuleNames:
    return GatewayImportModuleNames()


@pytest.fixture()
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


@pytest.fixture()
def import_gateway_models(isolated_gateway_model_import_modules):
    """Import gateway models from a clean module state."""

    def import_models():
        return importlib.import_module(GATEWAY_MODEL_MODULE)

    return import_models
