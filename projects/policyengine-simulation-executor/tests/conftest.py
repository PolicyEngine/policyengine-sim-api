"""Root pytest configuration for simulation API tests."""

import importlib
import sys
from contextlib import nullcontext
from pathlib import Path

import pytest

pytest_plugins = ()

project_root = Path(__file__).parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

ensure_project_root_on_path = importlib.import_module(
    "fixtures.test_support"
).ensure_project_root_on_path

ensure_project_root_on_path()


class NoOpObservabilityRuntime:
    """Minimal explicit runtime used by executor unit tests."""

    def span(self, *_args, **_kwargs):
        return nullcontext()

    def operation(self, *_args, **_kwargs):
        return nullcontext()

    def set_context(self, **_attributes):
        return None

    def event(self, *_args, **_kwargs):
        return None

    def record_exception(self, *_args, **_kwargs):
        return None

    def capture_context(self):
        return {
            "captured_at": "2026-09-22T00:00:00Z",
            "request_id": "test-request",
        }


@pytest.fixture
def observability_runtime():
    return NoOpObservabilityRuntime()


@pytest.fixture
def real_observability_runtime(monkeypatch):
    """Build the observability runtime the services build, exporting nothing.

    Its span and operation scopes are the ones deployed code opens, so a test
    can check what a block inside them returns or raises. OpenTelemetry is
    disabled and Google Cloud logging is not configured.
    """
    from policyengine_simulation_observability.observability import build_runtime

    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    monkeypatch.delenv("OBSERVABILITY_LOGGING_PROJECT_ID", raising=False)
    monkeypatch.delenv("OBSERVABILITY_LOG_NAME", raising=False)
    runtime = build_runtime(
        service_name="policyengine-simulation-executor-test",
        service_role="test",
        platform="local",
        environment="test",
    )
    yield runtime
    runtime.shutdown()


@pytest.fixture
def recorded_observability_runtime():
    """Build an observability runtime that keeps every record it writes.

    Yields the runtime and the list its structured records are appended to,
    in the order they were written. OpenTelemetry is disabled and nothing is
    exported.
    """
    from policyengine_observability import (
        CustomLogDestination,
        DeploymentIdentity,
        LoggingConfig,
        ObservabilityConfig,
        OTelConfig,
        ServiceIdentity,
        configure,
    )

    written_records: list[dict] = []

    class ListRecordWriter:
        def write(self, record: dict) -> None:
            written_records.append(record)

    runtime = configure(
        ObservabilityConfig(
            service=ServiceIdentity(
                name="policyengine-simulation-executor-test",
                namespace="policyengine.test",
                version="test",
                role="test",
            ),
            deployment=DeploymentIdentity(environment="test", platform="local"),
            logging=LoggingConfig(
                destinations=(
                    CustomLogDestination(
                        name="test-records",
                        writer_factory=ListRecordWriter,
                        delivery="inline",
                    ),
                ),
            ),
            otel=OTelConfig(enabled=False),
        )
    )
    yield runtime, written_records
    runtime.shutdown()


@pytest.fixture(autouse=True)
def _forget_spm_runtime_caches():
    """Keep the SPM runtime memoization out of every other test's way.

    ``runtime_spm_capability`` and the selection prevalidation are memoized
    on the installed environment, which a container cannot change but a test
    can. Clearing on both sides of each test means a stub is never served a
    previous test's answer, and never leaves one behind.
    """
    from policyengine_simulation_executor.spm import reset_spm_runtime_caches

    reset_spm_runtime_caches()
    yield
    reset_spm_runtime_caches()
