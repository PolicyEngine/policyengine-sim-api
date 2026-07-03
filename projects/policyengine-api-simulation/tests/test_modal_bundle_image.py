import importlib
import sys
from pathlib import Path
from types import ModuleType


def requirements_package_names(path):
    """Package names pinned in an exported requirements file."""
    return {
        line.split(";")[0].split("==")[0].strip()
        for line in Path(path).read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }


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
        self.calls.append(
            ("pip_install_from_requirements", requirements_txt, kwargs)
        )
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


def install_fake_modal(monkeypatch):
    modal = ModuleType("modal")
    modal.Image = FakeImage
    modal.Secret = FakeSecret
    modal.App = FakeApp
    modal.is_local = lambda: True
    modal.asgi_app = lambda: lambda function: function
    monkeypatch.setitem(sys.modules, "modal", modal)


def test_modal_image_uses_policyengine_bundle_install(monkeypatch):
    install_fake_modal(monkeypatch)
    monkeypatch.setenv("POLICYENGINE_VERSION", "4.19.1")
    monkeypatch.setenv("POLICYENGINE_CORE_VERSION", "3.27.1")
    monkeypatch.setenv("POLICYENGINE_US_VERSION", "1.700.0")
    monkeypatch.setenv("POLICYENGINE_UK_VERSION", "2.90.0")
    sys.modules.pop("src.modal.app", None)

    app = importlib.import_module("src.modal.app")

    command_calls = [
        call for call in app.simulation_image.calls if call[0] == "run_commands"
    ]
    assert command_calls
    command = command_calls[0][1][0]
    assert command.startswith(
        "uvx --from policyengine==4.19.1 policyengine bundle install 4.19.1"
    )
    assert "--python /usr/local/bin/python" in command
    assert "--data-dir /opt/policyengine/data" in command
    assert app.VERSION_ENV["POLICYENGINE_DATA_FOLDER"] == "/opt/policyengine/data"
    assert app.VERSION_ENV["POLICYENGINE_BUNDLE_RECEIPT"].endswith(
        "/.policyengine-bundle-receipt.json"
    )
    assert command_calls[0][2]["secrets"] == [app.data_secret, app.hf_secret]
    requirements_calls = [
        call
        for call in app.simulation_image.calls
        if call[0] == "pip_install_from_requirements"
    ]
    assert requirements_calls
    requirements_path = Path(requirements_calls[0][1])
    assert requirements_path.name == "modal-simulation-image.txt"
    packages = requirements_package_names(requirements_path)
    assert "policyengine-observability" in packages
    assert "logfire" in packages
    # logfire needs importlib_metadata at import time on Python 3.13 but
    # does not declare it; the pinned export must keep providing it or
    # every worker crashes on ``import logfire``.
    assert "importlib-metadata" in packages
    # uvx drives the policyengine bundle install into the image.
    assert "uv" in packages

    runtime_secret_sets = {
        name: kwargs["secrets"] for name, kwargs in app.app.function_calls
    }
    for function_name in ("run_simulation", "run_budget_window_batch"):
        assert runtime_secret_sets[function_name] == [
            app.gcp_secret,
            app.data_secret,
            app.hf_secret,
            app.logfire_secret,
        ]


# TEMPORARY: remove once single-year datasets are published (issue #596).
def test_modal_image_prebuilds_datasets_between_env_and_local_source(monkeypatch):
    install_fake_modal(monkeypatch)
    monkeypatch.setenv("POLICYENGINE_VERSION", "4.19.1")
    monkeypatch.setenv("POLICYENGINE_CORE_VERSION", "3.27.1")
    monkeypatch.setenv("POLICYENGINE_US_VERSION", "1.700.0")
    monkeypatch.setenv("POLICYENGINE_UK_VERSION", "2.90.0")
    sys.modules.pop("src.modal.app", None)

    app = importlib.import_module("src.modal.app")

    calls = app.simulation_image.calls
    prebuild_indices = [
        index
        for index, call in enumerate(calls)
        if call[0] == "run_function" and call[1] == "prebuild_country_datasets"
    ]
    # US only — UK is deliberately not prebuilt (keeps image build short).
    assert [calls[index][2]["args"] for index in prebuild_indices] == [("us",)]
    prebuild_kwargs = calls[prebuild_indices[0]][2]
    assert prebuild_kwargs["secrets"] == [app.data_secret, app.hf_secret]
    assert prebuild_kwargs["timeout"] == 4 * 60 * 60
    assert prebuild_kwargs["memory"] == 65536

    # The prebuild layer is a multi-hour build keyed only on upstream
    # layers and its own definition. It must stay after the version env
    # (so version bumps rebuild it) and before add_local_python_source
    # (whose content-hash key would otherwise invalidate it on every
    # source commit).
    env_index = next(index for index, call in enumerate(calls) if call[0] == "env")
    local_source_index = next(
        index
        for index, call in enumerate(calls)
        if call[0] == "add_local_python_source"
    )
    snapshot_index = next(
        index
        for index, call in enumerate(calls)
        if call[0] == "run_function" and call[1] == "snapshot_models"
    )
    assert env_index < prebuild_indices[0] < local_source_index < snapshot_index


def test_gateway_image_installs_dual_observability(monkeypatch):
    install_fake_modal(monkeypatch)
    sys.modules.pop("src.modal.gateway.app", None)

    app = importlib.import_module("src.modal.gateway.app")

    requirements_calls = [
        call
        for call in app.gateway_image.calls
        if call[0] == "pip_install_from_requirements"
    ]
    assert requirements_calls
    requirements_path = Path(requirements_calls[0][1])
    assert requirements_path.name == "modal-gateway-image.txt"
    packages = requirements_package_names(requirements_path)
    assert "policyengine-observability" in packages
    assert "logfire" in packages
    # Same importlib_metadata gap as the simulation image: without this the
    # gateway ASGI factory dies in configure_logfire and every request 303s.
    assert "importlib-metadata" in packages
    assert "pyjwt" in packages
    assert "cryptography" in packages

    function_kwargs = {name: kwargs for name, kwargs in app.app.function_calls}
    assert function_kwargs["web_app"]["secrets"] == [
        app.gateway_auth_secret,
        app.logfire_secret,
    ]
