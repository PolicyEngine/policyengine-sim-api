from __future__ import annotations

import pytest
from conftest import make_settings
from policyengine_simulation_entry.config import ConfigurationError, Settings


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


def test_permission_enforcement_defaults_to_log_only(monkeypatch):
    from policyengine_simulation_entry.config import Settings

    monkeypatch.delenv("SIMULATION_ENTRYPOINT_ENFORCE_PERMISSIONS", raising=False)
    assert Settings.from_env().enforce_permissions is False


@pytest.mark.parametrize(("value", "expected"), [("0", False), ("1", True)])
def test_permission_enforcement_accepts_zero_or_one(monkeypatch, value, expected):
    from policyengine_simulation_entry.config import Settings

    monkeypatch.setenv("SIMULATION_ENTRYPOINT_ENFORCE_PERMISSIONS", value)
    assert Settings.from_env().enforce_permissions is expected


@pytest.mark.parametrize("value", ["", "true", "yes", "2", " 1"])
def test_permission_enforcement_rejects_every_value_other_than_zero_or_one(
    monkeypatch,
    value,
):
    from policyengine_simulation_entry.config import Settings

    monkeypatch.setenv("SIMULATION_ENTRYPOINT_ENFORCE_PERMISSIONS", value)
    with pytest.raises(
        ConfigurationError,
        match="SIMULATION_ENTRYPOINT_ENFORCE_PERMISSIONS must be exactly 0 or 1",
    ):
        Settings.from_env()


def test_enabled_stage12_requires_independent_runtime_configuration():
    with pytest.raises(ConfigurationError, match="STAGE12_ENABLED=1 requires"):
        make_settings(stage12_enabled=True).validate()


def test_enabled_stage12_accepts_complete_independent_configuration():
    settings = make_settings(
        stage12_enabled=True,
        stage12_v2_manifest_environment="staging",
        stage12_database_url=(
            "postgresql://policyengine_v2_runtime:secret@"
            "test.pooler.supabase.com:5432/postgres?sslmode=require"
        ),
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
            stage12_database_url=(
                "postgresql://policyengine_v2_runtime:secret@"
                "test.pooler.supabase.com:5432/postgres?sslmode=require"
            ),
        ).validate()


@pytest.mark.parametrize(
    "database_url",
    [
        "postgresql+psycopg://policyengine_v2_runtime:secret@db.example/postgres",
        "https://db.example/postgres",
    ],
)
def test_stage12_database_url_requires_the_canonical_scheme(database_url):
    with pytest.raises(ConfigurationError, match="canonical postgresql"):
        make_settings(
            stage12_v2_manifest_environment="staging",
            stage12_database_url=database_url,
            stage12_artifact_bucket="policyengine-stage12-staging",
        ).validate()


def test_stage12_retention_is_fixed_at_thirty_days():
    with pytest.raises(ConfigurationError, match="exactly 30"):
        make_settings(stage12_retention_days=31).validate()


STAGE12_DATABASE_URL = (
    "postgresql://policyengine_v2_runtime:secret@"
    "test.pooler.supabase.com:5432/postgres?sslmode=require"
)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        pytest.param(
            {
                "environment": "prod",
                "auth_required": False,
                "auth_audience": "",
                "old_gateway_url": "",
            },
            "Caller authentication cannot be disabled in production.",
            id="production-auth-first",
        ),
        pytest.param(
            {"auth_issuer": "", "old_gateway_url": ""},
            "Set both SIMULATION_ENTRYPOINT_AUTH_ISSUER and "
            "SIMULATION_ENTRYPOINT_AUTH_AUDIENCE.",
            id="issuer-without-audience",
        ),
        pytest.param(
            {"auth_issuer": "", "auth_audience": "", "old_gateway_url": ""},
            "Caller authentication is required but issuer/audience are missing.",
            id="required-auth-without-issuer",
        ),
        pytest.param(
            {"auth_issuer": "https://issuer.example/?x=1", "old_gateway_url": ""},
            "SIMULATION_ENTRYPOINT_AUTH_ISSUER must not contain parameters, a "
            "query string, or a fragment.",
            id="caller-issuer-before-backend",
        ),
        pytest.param(
            {
                "old_gateway_url": "",
                "old_gateway_auth_client_id": "",
                "old_gateway_auth_client_secret": "",
            },
            "Missing old-gateway configuration: OLD_GATEWAY_URL, "
            "OLD_GATEWAY_AUTH_CLIENT_ID, OLD_GATEWAY_AUTH_CLIENT_SECRET.",
            id="missing-backend",
        ),
        pytest.param(
            {
                "old_gateway_auth_issuer": "http://issuer.example/",
                "connect_timeout_seconds": 0,
            },
            "OLD_GATEWAY_AUTH_ISSUER must be an absolute HTTPS URL.",
            id="backend-issuer-before-timeouts",
        ),
        pytest.param(
            {"connect_timeout_seconds": 0, "old_gateway_url": "http://x.modal.run"},
            "Old-gateway timeouts must be positive.",
            id="zero-connect-timeout",
        ),
        pytest.param(
            {"request_timeout_seconds": -1.0},
            "Old-gateway timeouts must be positive.",
            id="negative-request-timeout",
        ),
        pytest.param(
            {
                "old_gateway_url": "https://gateway.example",
                "stage12_v2_manifest_name": "simulation-api-routing-state",
            },
            "OLD_GATEWAY_URL must point to the existing Modal gateway.",
            id="gateway-host-before-stage12",
        ),
        pytest.param(
            {
                "stage12_retention_days": 29,
                "stage12_v2_manifest_environment": "staging",
            },
            "STAGE12_RETENTION_DAYS must be exactly 30.",
            id="retention-before-resources",
        ),
        pytest.param(
            {"stage12_artifact_bucket": "bucket", "stage12_enabled": True},
            "Stage 12 resources require: STAGE12_V2_MANIFEST_ENVIRONMENT, "
            "STAGE12_DATABASE_URL.",
            id="partial-resources-before-enabled",
        ),
        pytest.param(
            {
                "stage12_v2_manifest_environment": "staging",
                "stage12_database_url": "mysql://db.example/stage12",
                "stage12_artifact_bucket": "bucket",
            },
            "STAGE12_DATABASE_URL must use the canonical postgresql:// scheme.",
            id="database-scheme",
        ),
    ],
)
def test_validation_reports_the_first_failed_check(overrides, message):
    with pytest.raises(ConfigurationError) as error:
        make_settings(**overrides).validate()

    assert str(error.value) == message


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({}, id="complete"),
        pytest.param(
            {"auth_required": False, "auth_issuer": "", "auth_audience": ""},
            id="local-without-caller-auth",
        ),
        pytest.param(
            {
                "stage12_enabled": True,
                "stage12_v2_manifest_environment": "staging",
                "stage12_database_url": STAGE12_DATABASE_URL,
                "stage12_artifact_bucket": "bucket",
            },
            id="stage12-enabled",
        ),
        pytest.param(
            {
                "stage12_v2_manifest_environment": "staging",
                "stage12_database_url": STAGE12_DATABASE_URL,
                "stage12_artifact_bucket": "bucket",
            },
            id="stage12-configured-but-disabled",
        ),
    ],
)
def test_consistent_configuration_validates(overrides):
    make_settings(**overrides).validate()


def test_the_simulation_database_is_optional(monkeypatch):
    from policyengine_simulation_entry.config import Settings

    monkeypatch.delenv("SIMULATION_DATABASE_URL", raising=False)
    settings = Settings.from_env()

    assert settings.simulation_database_url == ""
    make_settings(simulation_database_url="").validate()


def test_the_simulation_database_url_is_read_from_the_environment(monkeypatch):
    from policyengine_simulation_entry.config import Settings

    monkeypatch.setenv(
        "SIMULATION_DATABASE_URL",
        "postgresql://simulation_runtime:secret@db.example:5432/postgres",
    )

    assert Settings.from_env().simulation_database_url == (
        "postgresql://simulation_runtime:secret@db.example:5432/postgres"
    )


@pytest.mark.parametrize(
    "database_url",
    [
        "postgres://simulation_runtime:secret@db.example/postgres",
        "postgresql+psycopg://simulation_runtime:secret@db.example/postgres",
        "mysql://simulation_runtime:secret@db.example/postgres",
    ],
)
def test_the_simulation_database_url_must_be_canonical(database_url):
    with pytest.raises(ConfigurationError, match="SIMULATION_DATABASE_URL") as error:
        make_settings(simulation_database_url=database_url).validate()

    assert "secret" not in str(error.value)


def test_call_reports_are_off_without_a_modal_workspace(monkeypatch):
    monkeypatch.delenv("SIMULATION_CALL_REPORT_MODAL_WORKSPACE_ID", raising=False)
    monkeypatch.delenv("SIMULATION_CALL_REPORT_MODAL_ENVIRONMENT", raising=False)

    settings = Settings.from_env()

    assert settings.expected_modal_caller is None


def test_call_reports_expect_the_configured_workspace_and_environment(monkeypatch):
    monkeypatch.setenv("SIMULATION_CALL_REPORT_MODAL_WORKSPACE_ID", "ac-workspace0001")
    monkeypatch.setenv("SIMULATION_CALL_REPORT_MODAL_ENVIRONMENT", "staging")

    expected = Settings.from_env().expected_modal_caller

    assert expected is not None
    assert expected.workspace_id == "ac-workspace0001"
    assert expected.environment_name == "staging"


@pytest.mark.parametrize(
    ("workspace", "environment"),
    [
        ("ac-workspace0001", ""),
        ("", "staging"),
        ("workspace0001", "staging"),
        ("ac-workspace0001", "Not An Environment"),
    ],
    ids=["no-environment", "no-workspace", "not-a-workspace-id", "bad-environment"],
)
def test_call_report_settings_must_name_one_modal_workspace_and_environment(
    workspace, environment
):
    with pytest.raises(ConfigurationError, match="SIMULATION_CALL_REPORT"):
        make_settings(
            call_report_modal_workspace_id=workspace,
            call_report_modal_environment=environment,
        ).validate()
