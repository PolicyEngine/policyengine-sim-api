"""Process-wide registry of shutdown callbacks run at the end of the app lifespan.

atexit handlers do not run reliably under uvicorn/FastAPI, and FastAPI has no
middleware-style way to register lifecycle events, so callbacks are registered
here and run from the app's lifespan instead.
"""

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any, TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T", bound=Callable[..., Any])


class AppExit:
    """Collect callbacks to run once, in registration order, when the app shuts down."""

    def __init__(self) -> None:
        """Start with no registered callbacks."""
        self.callbacks: list[Callable[[], None]] = []

    def __call__(self, *args: object, **kwargs: object) -> Callable[[T], T]:
        """Return a decorator that registers a function to run with these args at exit.

        The decorated function is returned unchanged.
        """

        def accept(func: T) -> T:
            self.callbacks.append(lambda: func(*args, **kwargs))
            return func

        return accept

    def _exit(self) -> None:
        log.info("Executing exit callbacks")
        for cb in self.callbacks:
            cb()
        log.info("Exit callbacks executed")

    @asynccontextmanager
    async def lifespan(self) -> AsyncIterator[None]:
        """Run every registered callback once the lifespan body exits without error."""
        yield
        self._exit()


exit = AppExit()
