"""
atexit does not work with uvicorn/fastapi for whatever reason and fastapi does not provide
a middleware-style way to register lifecycle events so I'm writing this
"""

import logging
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any, TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T", bound=Callable[..., Any])


class AppExit:
    def __init__(self):
        self.callbacks: list[Callable[[], None]] = []

    def __call__(self, *args: Any, **kwargs: Any):
        def accept(func: T) -> T:
            self.callbacks.append(lambda: func(*args, **kwargs))
            return func

        return accept

    def _exit(self):
        log.info("Executing exit callbacks")
        for cb in self.callbacks:
            cb()
        log.info("Exit callbacks executed")

    @asynccontextmanager
    async def lifespan(self):
        yield
        self._exit()


exit = AppExit()
