"""Explicit synchronization for the small testing-only concurrency check."""

import time
from collections.abc import Callable


def synchronize_testing_workers(
    publish_ready: Callable[[], None],
    both_ready: Callable[[], bool],
    *,
    timeout_seconds: float = 60,
    poll_interval_seconds: float = 10,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Fail if both real workers cannot be active within a bounded interval.

    Publish only after the independent sampler is running. This interval belongs
    to the dry test, not to year preparation or any production request.
    """
    deadline = clock() + timeout_seconds
    publish_ready()
    while not both_ready():
        if clock() >= deadline:
            raise TimeoutError("Second testing worker did not become ready")
        sleep(poll_interval_seconds)
