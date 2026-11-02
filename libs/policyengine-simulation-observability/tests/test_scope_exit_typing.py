"""The installed policyengine-observability types its scopes as never suppressing.

The services open spans and operations with ``runtime.span(...)`` and
``runtime.operation(...)``. A type checker reads an ``__exit__`` or
``__aexit__`` annotated as returning ``bool`` as "this context manager may
suppress the exception raised in its block", so Pyright would then report every
variable such a block assigns as possibly unbound after it, and every function
that returns from inside the block as possibly not returning.
policyengine-observability 3.0.4 annotates both methods as returning
``Literal[False]``. These tests fail if an older release is installed.
"""

from typing import Literal, get_type_hints

import pytest
from policyengine_simulation_observability.observability import build_runtime


@pytest.fixture
def runtime(monkeypatch):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    real_runtime = build_runtime(
        service_name="policyengine-simulation-scope-typing-test",
        service_role="test",
        platform="local",
        environment="test",
    )
    yield real_runtime
    real_runtime.shutdown()


@pytest.mark.parametrize("exit_method_name", ["__exit__", "__aexit__"])
@pytest.mark.parametrize(
    "open_scope",
    [
        pytest.param(lambda runtime: runtime.span("scope_typing"), id="span"),
        pytest.param(lambda runtime: runtime.operation("scope_typing"), id="operation"),
    ],
)
def test_scope_exit_is_typed_as_never_suppressing_an_exception(
    runtime, open_scope, exit_method_name
):
    scope_manager = open_scope(runtime)
    exit_method = getattr(type(scope_manager), exit_method_name)

    assert get_type_hints(exit_method)["return"] == Literal[False]
