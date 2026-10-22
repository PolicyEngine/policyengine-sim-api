"""The observability runtime's span scope, typed as never suppressing an exception.

policyengine-observability annotates its scope managers' ``__exit__`` as
returning ``bool``, although it always returns ``False``. A type checker reads a
``bool`` result as "this context manager may suppress the exception raised in
its block", so after ``with runtime.span(...)`` Pyright reports every variable
the block assigned as possibly unbound, and a function that returns from inside
the block as missing a return.

``span`` returns the runtime's own scope unchanged, typed as a context manager
whose ``__exit__`` returns ``bool | None``: the type ``contextlib.contextmanager``
scopes have, which type checkers don't treat as suppressing exceptions.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping
    from contextlib import AbstractContextManager

    from policyengine_observability import ObservabilityRuntime


def span(
    runtime: ObservabilityRuntime,
    name: str,
    *,
    attributes: Mapping[str, Any] | None = None,
) -> AbstractContextManager[object]:
    """Return ``runtime.span(name, attributes=attributes)``.

    The scope times its block as a child span, records an exception raised in
    the block, and lets that exception propagate.
    """
    return runtime.span(name, attributes=attributes)
