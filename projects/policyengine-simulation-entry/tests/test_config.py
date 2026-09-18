from __future__ import annotations

import pytest
from conftest import make_settings

from policyengine_simulation_entry.config import ConfigurationError


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


def test_stage12_automatic_execution_defaults_to_disabled(monkeypatch):
    from policyengine_simulation_entry.config import Settings

    monkeypatch.delenv("STAGE12_ENABLED", raising=False)
    settings = Settings.from_env()
    assert settings.stage12_enabled is False
    assert settings.stage12_resources_configured is False


@pytest.mark.parametrize("value", ["", "true", "yes", "2", " 1"])
def test_stage12_enabled_rejects_every_value_other_than_zero_or_one(monkeypatch, value):
    from policyengine_simulation_entry.config import Settings

    monkeypatch.setenv("STAGE12_ENABLED", value)
    with pytest.raises(ConfigurationError, match="exactly 0 or 1"):
        Settings.from_env()


def test_enabled_stage12_requires_independent_runtime_configuration():
    with pytest.raises(ConfigurationError, match="STAGE12_ENABLED=1 requires"):
        make_settings(stage12_enabled=True).validate()


def test_enabled_stage12_accepts_complete_independent_configuration():
    settings = make_settings(
        stage12_enabled=True,
        stage12_v2_manifest_environment="staging",
        stage12_database_url="postgresql://stage12-runtime",
        stage12_artifact_bucket="policyengine-stage12-staging",
    )
    settings.validate()
    assert settings.stage12_resources_configured is True


def test_stage12_manifest_must_not_reuse_v1_storage_name():
    with pytest.raises(ConfigurationError, match="different storage names"):
        make_settings(
            stage12_v2_manifest_name="simulation-api-routing-state"
        ).validate()


def test_partial_stage12_resource_configuration_is_rejected():
    with pytest.raises(ConfigurationError, match="Stage 12 resources require"):
        make_settings(
            stage12_v2_manifest_environment="staging",
            stage12_database_url="postgresql://stage12-runtime",
        ).validate()


def test_stage12_retention_is_fixed_at_thirty_days():
    with pytest.raises(ConfigurationError, match="exactly 30"):
        make_settings(stage12_retention_days=31).validate()
