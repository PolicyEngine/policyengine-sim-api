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


def test_stage12_comparison_backend_defaults_to_unconfigured(monkeypatch):
    from policyengine_simulation_entry.config import Settings

    monkeypatch.delenv("STAGE12_COMPARISON_BACKEND_CONFIGURED", raising=False)
    settings = Settings.from_env()
    assert settings.stage12_comparison_backend_configured is False
    assert settings.stage12_dispatch_max_in_flight == 8
    assert settings.stage12_dispatch_queue_capacity == 32
    assert settings.stage12_dispatch_timeout_seconds == 15


def test_enabled_stage12_requires_independent_runtime_configuration():
    with pytest.raises(ConfigurationError, match="Dual execution requires"):
        make_settings(stage12_comparison_backend_configured=True).validate()


def test_enabled_stage12_accepts_complete_independent_configuration():
    make_settings(
        stage12_comparison_backend_configured=True,
        stage12_v2_manifest_environment="staging",
        stage12_database_url="postgresql://stage12-runtime",
        stage12_artifact_bucket="policyengine-stage12-staging",
    ).validate()


def test_stage12_manifest_must_not_reuse_v1_storage_name():
    with pytest.raises(ConfigurationError, match="different storage names"):
        make_settings(
            stage12_v2_manifest_name="simulation-api-routing-state"
        ).validate()


def test_enabled_stage12_requires_a_runtime_control_name():
    with pytest.raises(ConfigurationError, match="STAGE12_CONTROL_NAME"):
        make_settings(
            stage12_comparison_backend_configured=True,
            stage12_control_name="",
            stage12_v2_manifest_environment="staging",
            stage12_database_url="postgresql://stage12-runtime",
            stage12_artifact_bucket="policyengine-stage12-staging",
        ).validate()


def test_stage12_retention_is_fixed_at_thirty_days():
    with pytest.raises(ConfigurationError, match="exactly 30"):
        make_settings(stage12_retention_days=31).validate()


@pytest.mark.parametrize(
    ("setting", "value", "expected_message"),
    [
        ("stage12_dispatch_max_in_flight", 0, "MAX_IN_FLIGHT"),
        ("stage12_dispatch_max_in_flight", 33, "MAX_IN_FLIGHT"),
        ("stage12_dispatch_queue_capacity", 0, "QUEUE_CAPACITY"),
        ("stage12_dispatch_queue_capacity", 1_001, "QUEUE_CAPACITY"),
        ("stage12_dispatch_timeout_seconds", 0, "TIMEOUT_SECONDS"),
        ("stage12_dispatch_timeout_seconds", 61, "TIMEOUT_SECONDS"),
    ],
)
def test_stage12_dispatch_limits_are_bounded(setting, value, expected_message):
    with pytest.raises(ConfigurationError, match=expected_message):
        make_settings(**{setting: value}).validate()
