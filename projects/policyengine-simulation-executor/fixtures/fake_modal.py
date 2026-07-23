"""Shared fake `modal` module for image/app declaration tests.

Extracted from test_modal_bundle_image so every test lane asserting Modal
image layers or function declarations uses one recording fake instead of
a per-file copy. The fakes record calls; they run nothing.
"""

import sys
from types import ModuleType


class FakeImage:
    def __init__(self):
        self.calls = []

    @classmethod
    def debian_slim(cls, python_version):
        image = cls()
        image.calls.append(("debian_slim", python_version))
        return image

    def pip_install(self, *packages):
        self.calls.append(("pip_install", packages))
        return self

    def pip_install_from_requirements(self, requirements_txt, **kwargs):
        self.calls.append(("pip_install_from_requirements", requirements_txt, kwargs))
        return self

    def uv_sync(self, uv_project_dir="./", **kwargs):
        self.calls.append(("uv_sync", uv_project_dir, kwargs))
        return self

    def run_commands(self, *commands, **kwargs):
        self.calls.append(("run_commands", commands, kwargs))
        return self

    def env(self, env):
        self.calls.append(("env", env))
        return self

    def add_local_python_source(self, *args, **kwargs):
        self.calls.append(("add_local_python_source", args, kwargs))
        return self

    def run_function(self, function, **kwargs):
        self.calls.append(("run_function", function.__name__, kwargs))
        return self


class FakeSecret:
    @staticmethod
    def from_name(*args, **kwargs):
        return {"args": args, "kwargs": kwargs}


class FakeApp:
    def __init__(self, name):
        self.name = name
        self.function_calls = []

    def function(self, **kwargs):
        def decorator(function):
            self.function_calls.append((function.__name__, kwargs))
            return function

        return decorator

    def local_entrypoint(self, **kwargs):
        def decorator(function):
            return function

        return decorator


def install_fake_modal(monkeypatch):
    modal = ModuleType("modal")
    modal.Image = FakeImage
    modal.Secret = FakeSecret
    modal.App = FakeApp
    modal.is_local = lambda: True
    modal.asgi_app = lambda: lambda function: function
    monkeypatch.setitem(sys.modules, "modal", modal)
