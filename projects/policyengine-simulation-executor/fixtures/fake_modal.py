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
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    @classmethod
    def debian_slim(cls, python_version: str) -> Self:
        image = cls()
        image.calls.append(("debian_slim", python_version))
        return image

    def pip_install(self, *packages: str) -> Self:
        self.calls.append(("pip_install", packages))
        return self

    def pip_install_from_requirements(
        self, requirements_txt: str, **kwargs: object
    ) -> Self:
        self.calls.append(("pip_install_from_requirements", requirements_txt, kwargs))
        return self

    def uv_sync(self, uv_project_dir: str = "./", **kwargs: object) -> Self:
        self.calls.append(("uv_sync", uv_project_dir, kwargs))
        return self

    def run_commands(self, *commands: str, **kwargs: object) -> Self:
        self.calls.append(("run_commands", commands, kwargs))
        return self

    def env(self, env: Mapping[str, str]) -> Self:
        self.calls.append(("env", env))
        return self

    def add_local_python_source(self, *args: str, **kwargs: object) -> Self:
        self.calls.append(("add_local_python_source", args, kwargs))
        return self

    def add_local_file(
        self, local_path: str | Path, remote_path: str, **kwargs: object
    ) -> Self:
        self.calls.append(("add_local_file", local_path, remote_path, kwargs))
        return self

    def add_local_dir(
        self, local_path: str | Path, remote_path: str, **kwargs: object
    ) -> Self:
        self.calls.append(("add_local_dir", local_path, remote_path, kwargs))
        return self

    def run_function(self, function: Callable[..., object], **kwargs: object) -> Self:
        self.calls.append(("run_function", function.__name__, kwargs))
        return self


class FakeSecret:
    @staticmethod
    def from_name(*args: object, **kwargs: object) -> dict[str, object]:
        return {"args": args, "kwargs": kwargs}


class FakeApp:
    def __init__(self, name: str) -> None:
        self.name = name
        self.function_calls: list[tuple[str, dict[str, object]]] = []

    def function[F: Callable[..., object]](self, **kwargs: object) -> Callable[[F], F]:
        def decorator(function: F) -> F:
            self.function_calls.append((function.__name__, kwargs))
            return function

        return decorator

    def local_entrypoint[F: Callable[..., object]](
        self, **kwargs: object
    ) -> Callable[[F], F]:
        def decorator(function: F) -> F:
            return function

        return decorator


def install_fake_modal(monkeypatch: pytest.MonkeyPatch) -> None:
    modal = ModuleType("modal")
    modal.Image = FakeImage
    modal.Secret = FakeSecret
    modal.App = FakeApp
    modal.Cron = lambda schedule: {"schedule": schedule}
    modal.is_local = lambda: True
    modal.asgi_app = lambda: lambda function: function
    monkeypatch.setitem(sys.modules, "modal", modal)
