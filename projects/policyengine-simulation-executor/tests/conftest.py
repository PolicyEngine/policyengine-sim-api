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

    def span(self, *args, **kwargs):
        return nullcontext()

    def operation(self, *args, **kwargs):
        return nullcontext()

    def set_context(self, **attributes):
        return None

    def event(self, *args, **kwargs):
        return None

    def record_exception(self, *args, **kwargs):
        return None

    def capture_context(self):
        return {
            "captured_at": "2026-09-22T00:00:00Z",
            "request_id": "test-request",
        }


@pytest.fixture
def observability_runtime():
    return NoOpObservabilityRuntime()


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
