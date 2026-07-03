from policyengine_fastapi.observability import TracerCaptureMode
from policyengine_simulation_executor.settings import get_settings


def test_settings_default_observability_config_is_disabled():
    get_settings.cache_clear()
    try:
        settings = get_settings()
    finally:
        get_settings.cache_clear()

    config = settings.observability

    assert config.enabled is False
    assert config.shadow_mode is True
    assert config.service_name == "policyengine-simulation-executor"
    assert config.environment == settings.environment.value
    assert config.otlp_headers == {}
    assert config.tracer_capture_mode == TracerCaptureMode.DISABLED
    assert config.slow_run_threshold_seconds == 30.0


def test_settings_expose_observability_config(monkeypatch):
    monkeypatch.setenv("OBSERVABILITY_ENABLED", "true")
    monkeypatch.setenv("OBSERVABILITY_SHADOW_MODE", "false")
    monkeypatch.setenv("OBSERVABILITY_SERVICE_NAME", "simulation-worker")
    monkeypatch.setenv("OBSERVABILITY_ENVIRONMENT", "staging")
    monkeypatch.setenv("OBSERVABILITY_OTLP_ENDPOINT", "https://otlp.example")
    monkeypatch.setenv(
        "OBSERVABILITY_OTLP_HEADERS",
        "Authorization=Bearer abc,X-Scope=ops",
    )
    monkeypatch.setenv("OBSERVABILITY_ARTIFACT_BUCKET", "test-bucket")
    monkeypatch.setenv("OBSERVABILITY_ARTIFACT_PREFIX", "diagnostics")
    monkeypatch.setenv("OBSERVABILITY_TRACER_CAPTURE_MODE", "threshold")
    monkeypatch.setenv("OBSERVABILITY_SLOW_RUN_THRESHOLD_SECONDS", "45.5")

    get_settings.cache_clear()
    try:
        settings = get_settings()
    finally:
        get_settings.cache_clear()

    config = settings.observability

    assert config.enabled is True
    assert config.shadow_mode is False
    assert config.service_name == "simulation-worker"
    assert config.environment == "staging"
    assert config.otlp_endpoint == "https://otlp.example"
    assert config.otlp_headers == {
        "Authorization": "Bearer abc",
        "X-Scope": "ops",
    }
    assert config.artifact_bucket == "test-bucket"
    assert config.artifact_prefix == "diagnostics"
    assert config.tracer_capture_mode == TracerCaptureMode.THRESHOLD
    assert config.slow_run_threshold_seconds == 45.5
