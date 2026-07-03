"""Image-structure tests for the gateway Modal app (fake modal).

The gateway image must install ONLY from this project's uv.lock via
uv_sync(frozen=True) — no ad-hoc pip layers — so the image environment
is exactly what CI unit tests run against (issue #602's class fix).
"""

import importlib
import sys
from pathlib import Path
from types import ModuleType

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class FakeImage:
    def __init__(self):
        self.calls = []

    @classmethod
    def debian_slim(cls, python_version):
        image = cls()
        image.calls.append(("debian_slim", python_version))
        return image

    def uv_sync(self, uv_project_dir="./", **kwargs):
        self.calls.append(("uv_sync", uv_project_dir, kwargs))
        return self

    def pip_install(self, *packages, **kwargs):
        self.calls.append(("pip_install", packages, kwargs))
        return self

    def pip_install_from_requirements(self, requirements_txt, **kwargs):
        self.calls.append(
            ("pip_install_from_requirements", requirements_txt, kwargs)
        )
        return self

    def add_local_python_source(self, *args, **kwargs):
        self.calls.append(("add_local_python_source", args, kwargs))
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


def install_fake_modal(monkeypatch):
    modal = ModuleType("modal")
    modal.Image = FakeImage
    modal.Secret = FakeSecret
    modal.App = FakeApp
    modal.is_local = lambda: True
    modal.asgi_app = lambda: lambda function: function
    monkeypatch.setitem(sys.modules, "modal", modal)


def import_gateway_app(monkeypatch):
    install_fake_modal(monkeypatch)
    sys.modules.pop("policyengine_simulation_gateway.app", None)
    return importlib.import_module("policyengine_simulation_gateway.app")


def test_gateway_image_installs_from_own_lock_via_uv_sync(monkeypatch):
    app = import_gateway_app(monkeypatch)

    uv_sync_calls = [
        call for call in app.gateway_image.calls if call[0] == "uv_sync"
    ]
    assert len(uv_sync_calls) == 1
    _, uv_project_dir, kwargs = uv_sync_calls[0]
    # Absolute project dir: Modal resolves relative dirs against the
    # caller's cwd, and this module is imported from many cwds.
    assert Path(uv_project_dir) == PROJECT_ROOT
    assert kwargs["frozen"] is True
    # uv sync installs the dev group by default, and the dev group holds
    # ../../libs path deps that do not exist in Modal's build context.
    assert "--no-default-groups" in kwargs["extra_options"]

    # The lock is the only package source: any ad-hoc pip layer would
    # reintroduce build-time resolution (issue #602).
    assert not [
        call
        for call in app.gateway_image.calls
        if call[0] in ("pip_install", "pip_install_from_requirements")
    ]


def test_gateway_image_mounts_local_packages(monkeypatch):
    app = import_gateway_app(monkeypatch)

    mounts = [
        call
        for call in app.gateway_image.calls
        if call[0] == "add_local_python_source"
    ]
    # Dropping a package from this tuple crashes the gateway at import
    # time — the dev-group path deps are NOT installed in the image.
    assert mounts[0][1] == (
        "policyengine_simulation_gateway",
        "policyengine_simulation_contract",
        "policyengine_simulation_observability",
        "policyengine_fastapi",
    )


def test_web_app_secrets(monkeypatch):
    app = import_gateway_app(monkeypatch)

    function_kwargs = {name: kwargs for name, kwargs in app.app.function_calls}
    assert function_kwargs["web_app"]["secrets"] == [
        app.gateway_auth_secret,
        app.logfire_secret,
    ]
