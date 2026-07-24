from __future__ import annotations

import pytest

from policyengine_simulation_entrypoint.config import ConfigurationError

from conftest import make_settings


def test_production_refuses_disabled_auth():
    with pytest.raises(ConfigurationError, match="cannot be disabled"):
        make_settings(environment="production", auth_required=False).validate()


def test_partial_caller_auth_is_rejected():
    with pytest.raises(ConfigurationError, match="Set both"):
        make_settings(auth_audience="").validate()


def test_recursive_gateway_url_is_rejected():
    with pytest.raises(ConfigurationError, match="must not point"):
        make_settings(
            old_gateway_url="https://simulation.example.test",
        ).validate()


def test_complete_configuration_is_valid():
    make_settings().validate()
