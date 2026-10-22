"""Shared fake `modal` module for image/app declaration tests.

Extracted from test_modal_bundle_image so every test lane asserting Modal
image layers or function declarations uses one recording fake instead of
a per-file copy. The fakes record calls; they run nothing.
"""

from __future__ import annotations

import sys
from types import ModuleType
from typing import TYPE_CHECKING, Self

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

    import pytest


class FakeImage:
    """Recording stand-in for ``modal.Image``: each builder call appends to ``calls``.

    Every method returns the same image, so a chained build leaves one ordered
    list of ``(method_name, *arguments)`` tuples to assert layer order against.
    """

    def __init__(self) -> None:
        """Start with no recorded layers."""
        self.calls: list[tuple[object, ...]] = []

    @classmethod
    def debian_slim(cls, python_version: str) -> Self:
        """Return a new image whose first recorded call is the base layer."""
        image = cls()
        image.calls.append(("debian_slim", python_version))
        return image

    def apt_install(self, *packages: str) -> Self:
        """Record an ``apt_install`` layer with the package tuple."""
        self.calls.append(("apt_install", packages))
        return self

    def pip_install(self, *packages: str) -> Self:
        """Record a ``pip_install`` layer with the package tuple."""
        self.calls.append(("pip_install", packages))
        return self

    def pip_install_from_requirements(
        self, requirements_txt: str, **kwargs: object
    ) -> Self:
        """Record a requirements-file install layer."""
        self.calls.append(("pip_install_from_requirements", requirements_txt, kwargs))
        return self

    def uv_sync(self, uv_project_dir: str = "./", **kwargs: object) -> Self:
        """Record a ``uv_sync`` layer with its project dir and options."""
        self.calls.append(("uv_sync", uv_project_dir, kwargs))
        return self

    def run_commands(self, *commands: str, **kwargs: object) -> Self:
        """Record a ``run_commands`` layer with the command tuple and options."""
        self.calls.append(("run_commands", commands, kwargs))
        return self

    def env(self, env: Mapping[str, str]) -> Self:
        """Record an ``env`` layer with the mapping as passed."""
        self.calls.append(("env", env))
        return self

    def add_local_python_source(self, *args: str, **kwargs: object) -> Self:
        """Record the source packages added and their options (e.g. ``copy``)."""
        self.calls.append(("add_local_python_source", args, kwargs))
        return self

    def add_local_file(
        self, local_path: str | Path, remote_path: str, **kwargs: object
    ) -> Self:
        """Record a single file copied into the image."""
        self.calls.append(("add_local_file", local_path, remote_path, kwargs))
        return self

    def add_local_dir(
        self, local_path: str | Path, remote_path: str, **kwargs: object
    ) -> Self:
        """Record a directory copied into the image."""
        self.calls.append(("add_local_dir", local_path, remote_path, kwargs))
        return self

    def run_function(self, function: Callable[..., object], **kwargs: object) -> Self:
        """Record a build-time function layer by function name, without running it."""
        self.calls.append(("run_function", function.__name__, kwargs))
        return self


class FakeSecret:
    """Stand-in for ``modal.Secret`` that resolves nothing."""

    @staticmethod
    def from_name(*args: object, **kwargs: object) -> dict[str, object]:
        """Return the lookup arguments so tests can assert which secret was named."""
        return {"args": args, "kwargs": kwargs}


class InputCancellation(BaseException):
    """Stand-in for ``modal.exception.InputCancellation``.

    Modal raises it in a running synchronous input that is cancelled. It
    subclasses ``BaseException`` so that ``except Exception`` does not catch
    it. The project's ``src/modal`` package shadows the installed Modal package
    in unit tests, so the real class cannot be imported there.
    """


class FakeApp:
    """Stand-in for ``modal.App`` that records ``function`` decorator options."""

    def __init__(self, name: str) -> None:
        """Name the app and start with no recorded function declarations."""
        self.name = name
        self.function_calls: list[tuple[str, dict[str, object]]] = []

    def function[F: Callable[..., object]](self, **kwargs: object) -> Callable[[F], F]:
        """Return a decorator recording ``(name, kwargs)`` that returns the function."""

        def decorator(function: F) -> F:
            self.function_calls.append((function.__name__, kwargs))
            return function

        return decorator

    def local_entrypoint[F: Callable[..., object]](
        self, **_kwargs: object
    ) -> Callable[[F], F]:
        """Return a pass-through decorator; entrypoints are not recorded."""

        def decorator(function: F) -> F:
            return function

        return decorator


def install_fake_modal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install the recording fakes as ``sys.modules["modal"]`` for one test.

    ``is_local()`` reports True and ``asgi_app`` is a pass-through decorator.
    Modules that already imported the real ``modal`` must be re-imported.
    """
    modal = ModuleType("modal")
    modal.Image = FakeImage
    modal.Secret = FakeSecret
    modal.App = FakeApp
    modal.Cron = lambda schedule: {"schedule": schedule}
    modal.is_local = lambda: True
    modal.asgi_app = lambda: lambda function: function
    monkeypatch.setitem(sys.modules, "modal", modal)
