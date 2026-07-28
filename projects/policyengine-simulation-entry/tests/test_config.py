from __future__ import annotations

import pytest

from policyengine_simulation_entry.config import ConfigurationError

from conftest import make_settings


def test_production_refuses_disabled_auth():
    with pytest.raises(ConfigurationError, match="cannot be disabled"):
        make_settings(environment="production", auth_required=False).validate()


def test_partial_caller_auth_is_rejected():
    with pytest.raises(ConfigurationError, match="Set both"):
        make_settings(auth_audience="").validate()


@pytest.mark.parametrize(
    ("setting", "value"),
    [
        ("auth_issuer", "http://caller-auth.example/"),
        ("auth_issuer", "https://caller-auth.example/?tenant=wrong"),
        ("old_gateway_auth_issuer", "http://backend-auth.example/"),
        ("old_gateway_auth_issuer", "https://client:secret@backend-auth.example/"),
    ],
)
def test_auth_issuers_must_be_safe_https_urls(setting, value):
    with pytest.raises(ConfigurationError, match="must"):
        make_settings(**{setting: value}).validate()


@pytest.mark.parametrize(
    "old_gateway_url",
    [
        "https://policyengine-simulation-entry-abc-uc.a.run.app",
        "http://policyengine--policyengine-simulation-gateway-web-app.modal.run",
        "not-a-url",
        (
            "https://policyengine--policyengine-simulation-gateway-web-app.modal.run"
            "/unexpected-path"
        ),
        (
            "https://policyengine--policyengine-simulation-gateway-web-app.modal.run"
            "?wrong=path"
        ),
        (
            "https://policyengine--policyengine-simulation-gateway-web-app.modal.run"
            "#wrong-path"
        ),
    ],
)
def test_old_gateway_must_be_the_https_modal_service(old_gateway_url):
    with pytest.raises(ConfigurationError, match="OLD_GATEWAY_URL must"):
        make_settings(old_gateway_url=old_gateway_url).validate()


def test_complete_configuration_is_valid():
    make_settings().validate()
