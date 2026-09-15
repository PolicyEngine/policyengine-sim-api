import importlib
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from fixtures.fake_modal import install_fake_modal


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
        "PIP_CONSTRAINT=/opt/policyengine/bundle-constraints.txt "
        "uvx --from policyengine==4.19.1 policyengine bundle install 4.19.1"
    )
    constraint_call = next(
        call for call in app.simulation_image.calls if call[0] == "add_local_file"
    )
    assert constraint_call[2] == "/opt/policyengine/bundle-constraints.txt"
    assert constraint_call[3] == {"copy": True}
    assert app.simulation_image.calls.index(constraint_call) < (
        app.simulation_image.calls.index(command_calls[0])
    )
    constraints = Path(constraint_call[1]).read_text()
    # 4.19.1 is a historical route: its rebuild keeps the calculator that
    # bundle was reviewed with, not the canonical one this project now pins.
    assert "spm-calculator==0.3.1" in constraints.splitlines()
    # The bundle installs into uv_sync's venv so locked packages and
    # bundled models share one environment.
    assert "--venv /.uv/.venv" in command
    assert "--data-dir /opt/policyengine/data" in command
    assert app.VERSION_ENV["POLICYENGINE_DATA_FOLDER"] == "/opt/policyengine/data"
    assert app.VERSION_ENV["POLICYENGINE_BUNDLE_RECEIPT"].endswith(
        "/.policyengine-bundle-receipt.json"
    )
    assert command_calls[0][2]["secrets"] == [app.data_secret, app.hf_secret]
    uv_sync_calls = [
        call for call in app.simulation_image.calls if call[0] == "uv_sync"
    ]
    assert len(uv_sync_calls) == 1
    _, uv_project_dir, kwargs = uv_sync_calls[0]
    assert Path(uv_project_dir) == Path(__file__).resolve().parents[1]
    assert kwargs["frozen"] is True
    # Only the image dependency group — the project's heavyweight deps
    # (country models) arrive via the bundle install instead.
    assert "--only-group modal-simulation-image" in kwargs["extra_options"]
    # The lock is the only package source; ad-hoc pip layers would
    # reintroduce build-time resolution (issue #602).
    assert not [
        call
        for call in app.simulation_image.calls
        if call[0] in ("pip_install", "pip_install_from_requirements")
    ]

    group = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
    )["dependency-groups"]["modal-simulation-image"]
    names = {requirement.split(">=")[0].split("[")[0] for requirement in group}
    assert "policyengine-observability" in names
    assert "logfire" in names
    # logfire needs importlib_metadata at import time on Python 3.13 but
    # does not declare it; the group must keep providing it or every
    # worker crashes on ``import logfire``.
    assert "importlib-metadata" in names
    # uvx drives the policyengine bundle install into the image.
    assert "uv" in names

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


@pytest.mark.parametrize(
    "version,calculator",
    [
        ("4.18.3", "0.3.1"),
        ("4.18.5", "0.3.1"),
        ("4.18.7", "0.3.1"),
        ("4.18.8", "0.3.1"),
        ("4.18.9", "0.3.1"),
        ("4.19.1", "0.3.1"),
        ("4.20.3", "0.3.1"),
        ("4.22.0", "0.3.1"),
        ("5.2.0", "0.3.1"),
        ("5.3.0", "0.3.1"),
        ("6.0.0", "1.0.0"),
    ],
)
def test_bundle_command_passes_constraints_to_child_installer(
    monkeypatch, tmp_path, version, calculator
):
    """Every reviewed bundle rebuild inherits its own pip constraint."""
    install_fake_modal(monkeypatch)
    monkeypatch.setenv("POLICYENGINE_VERSION", version)
    monkeypatch.setenv("POLICYENGINE_CORE_VERSION", "3.30.1")
    monkeypatch.setenv("POLICYENGINE_US_VERSION", "1.764.6")
    monkeypatch.setenv("POLICYENGINE_UK_VERSION", "2.90.2")
    sys.modules.pop("src.modal.app", None)
    app = importlib.import_module("src.modal.app")
    constraint_path = tmp_path / "bundle-constraints.txt"
    constraint_path.write_text(f"spm-calculator=={calculator}\n")
    monkeypatch.setattr(app, "BUNDLE_CONSTRAINTS_PATH", str(constraint_path))
    executable = tmp_path / "uvx"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys\n"
        "print(json.dumps({'args': sys.argv[1:], 'constraints': "
        "pathlib.Path(os.environ['PIP_CONSTRAINT']).read_text()}))\n"
    )
    executable.chmod(0o755)
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"}
    result = subprocess.run(
        ["/bin/sh", "-c", app.bundle_install_command(version)],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    output = json.loads(result.stdout)
    assert output["constraints"] == f"spm-calculator=={calculator}\n"
    assert output["args"][:6] == [
        "--from",
        f"policyengine=={version}",
        "policyengine",
        "bundle",
        "install",
        version,
    ]


def test_unreviewed_bundle_cannot_inherit_legacy_calculator_constraint(monkeypatch):
    """A new bundle needs an explicit reviewed calculator selection."""
    install_fake_modal(monkeypatch)
    monkeypatch.setenv("POLICYENGINE_VERSION", "5.2.0")
    monkeypatch.setenv("POLICYENGINE_CORE_VERSION", "3.30.1")
    monkeypatch.setenv("POLICYENGINE_US_VERSION", "1.764.6")
    monkeypatch.setenv("POLICYENGINE_UK_VERSION", "2.90.2")
    sys.modules.pop("src.modal.app", None)
    app = importlib.import_module("src.modal.app")
    with pytest.raises(ValueError, match="reviewed calculator constraint"):
        app.bundle_install_command("unreviewed-future-bundle")


def test_canonical_bundle_has_its_own_reviewed_calculator_constraint(monkeypatch):
    """The canonical SPM migration adds a file; it does not rewrite history.

    ``BUNDLE_CONSTRAINT_FILES`` selects by exact bundle version and the
    selected file is the only thing pip reads during the bundle install, so
    a shared file would silently move every historical route's calculator.
    """
    install_fake_modal(monkeypatch)
    monkeypatch.setenv("POLICYENGINE_VERSION", "6.0.0")
    monkeypatch.setenv("POLICYENGINE_CORE_VERSION", "3.32.5")
    monkeypatch.setenv("POLICYENGINE_US_VERSION", "2.2.1")
    monkeypatch.setenv("POLICYENGINE_UK_VERSION", "2.90.2")
    sys.modules.pop("src.modal.app", None)
    app = importlib.import_module("src.modal.app")

    project_dir = Path(__file__).resolve().parents[1]
    canonical = project_dir / app.bundle_constraints_file("6.0.0")
    legacy = project_dir / app.bundle_constraints_file("5.2.0")
    assert canonical != legacy
    project = tomllib.loads((project_dir / "pyproject.toml").read_text())
    calculator_pin = next(
        dependency
        for dependency in project["project"]["dependencies"]
        if dependency.startswith("spm-calculator==")
    )
    # The image constraint and the locked project pin are one decision for
    # the bundle this project deploys.
    assert calculator_pin in canonical.read_text().splitlines()
    assert "spm-calculator==0.3.1" in legacy.read_text().splitlines()
    assert "spm-calculator==0.3.1" not in canonical.read_text().splitlines()

    # The deployed image copies the selected file to the one path pip reads.
    constraint_call = next(
        call for call in app.simulation_image.calls if call[0] == "add_local_file"
    )
    assert Path(constraint_call[1]) == canonical
    assert constraint_call[2] == app.BUNDLE_CONSTRAINTS_PATH


def _fake_manifest():
    """A schema-valid store payload: app.py validates what it reads, so
    the fake must satisfy ArtifactManifest — deriving it from the model
    keeps the test from drifting off the wire shape."""
    from policyengine_simulation_executor.precompute_models import ArtifactManifest

    return ArtifactManifest.model_validate(
        {
            "schema": "mf1",
            "country": "us",
            "receipt": {
                "policyengine_version": "4.19.1",
                "model_version": "1.700.0",
                "data_version": "1.2.3",
                "data_artifact_revision": "rev-abc",
                "default_dataset": "populace_cps",
            },
            "artifacts": [
                {
                    "type": "dataset",
                    "path": "datasets/us/d1/populace_year_2026.h5",
                    "filename": "populace_year_2026.h5",
                    "year": 2026,
                    "digest": "d1",
                },
            ],
        }
    ).canonical_payload()


def test_modal_image_fetches_artifacts_between_env_and_local_source(monkeypatch):
    install_fake_modal(monkeypatch)
    monkeypatch.setenv("POLICYENGINE_VERSION", "4.19.1")
    monkeypatch.setenv("POLICYENGINE_CORE_VERSION", "3.27.1")
    monkeypatch.setenv("POLICYENGINE_US_VERSION", "1.700.0")
    monkeypatch.setenv("POLICYENGINE_UK_VERSION", "2.90.0")
    monkeypatch.setenv("POLICYENGINE_MANIFEST_DIGEST", "digest-abc")
    monkeypatch.setenv("POLICYENGINE_ARTIFACT_BUCKET", "test-bucket")

    manifest = _fake_manifest()

    class FakeStore:
        def __init__(self, bucket_name=None, **kwargs):
            self.bucket_name = bucket_name

        def read_manifest(self, digest):
            assert digest == "digest-abc"
            return manifest

    # The app imports ArtifactStore lazily inside the digest-set branch,
    # so patching the module attribute is enough — no GCS involved.
    import policyengine_simulation_executor.artifact_store as artifact_store

    monkeypatch.setattr(artifact_store, "ArtifactStore", FakeStore)
    sys.modules.pop("src.modal.app", None)

    app = importlib.import_module("src.modal.app")

    calls = app.simulation_image.calls
    fetch_indices = [
        index
        for index, call in enumerate(calls)
        if call[0] == "run_function" and call[1] == "fetch_artifacts"
    ]
    assert len(fetch_indices) == 1
    fetch_kwargs = calls[fetch_indices[0]][2]
    # The manifest content is in the layer args: the layer cache key
    # follows the artifact set, nothing else.
    assert fetch_kwargs["args"] == ("test-bucket", manifest)
    assert fetch_kwargs["secrets"] == [app.gcp_secret]
    assert fetch_kwargs["cpu"] == 2.0
    assert fetch_kwargs["memory"] == 4096
    assert fetch_kwargs["timeout"] == 900

    # The fetch layer must stay after the version env (its freshness gate
    # reads the baked versions) and before add_local_python_source (whose
    # content-hash key would otherwise invalidate it on every source
    # commit).
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
    assert env_index < fetch_indices[0] < local_source_index < snapshot_index

    # The shared libs ship into the image as mounted source; dropping one
    # from this tuple crashes workers at import time.
    assert calls[local_source_index][1] == (
        "src.modal",
        "policyengine_simulation_executor",
        "policyengine_simulation_observability",
        "policyengine_simulation_contract",
    )


def test_modal_image_uses_failing_sentinel_without_manifest_digest(monkeypatch):
    """Without a digest the app must still import — the precompute and
    smoke apps import it where no digest can exist — but the fetch layer
    args must carry a None manifest, which makes fetch_artifacts fail the
    build loudly. A digest-less deploy cannot silently ship an
    artifact-less image."""
    install_fake_modal(monkeypatch)
    monkeypatch.setenv("POLICYENGINE_VERSION", "4.19.1")
    monkeypatch.setenv("POLICYENGINE_CORE_VERSION", "3.27.1")
    monkeypatch.setenv("POLICYENGINE_US_VERSION", "1.700.0")
    monkeypatch.setenv("POLICYENGINE_UK_VERSION", "2.90.0")
    monkeypatch.delenv("POLICYENGINE_MANIFEST_DIGEST", raising=False)
    monkeypatch.delenv("POLICYENGINE_ARTIFACT_BUCKET", raising=False)
    sys.modules.pop("src.modal.app", None)

    app = importlib.import_module("src.modal.app")

    fetch_calls = [
        call
        for call in app.simulation_image.calls
        if call[0] == "run_function" and call[1] == "fetch_artifacts"
    ]
    assert len(fetch_calls) == 1
    assert fetch_calls[0][2]["args"] == ("", None)


def test_app_module_imports_at_container_entrypoint_path(monkeypatch):
    """Modal loads the deployed function's module as /root/app.py.

    Module-level path math must survive that placement (parents[2] does
    not exist there) with modal.is_local() returning False — the exact
    setup that crash-looped the staging worker on first boot.
    """
    import importlib.util

    install_fake_modal(monkeypatch)
    sys.modules["modal"].is_local = lambda: False
    monkeypatch.setenv("POLICYENGINE_VERSION", "4.19.1")
    monkeypatch.setenv("POLICYENGINE_CORE_VERSION", "3.27.1")
    monkeypatch.setenv("POLICYENGINE_US_VERSION", "1.700.0")
    monkeypatch.setenv("POLICYENGINE_UK_VERSION", "2.90.0")
    # Containers must stay inert even if a digest leaks into their env:
    # the is_local() check comes before any env read or GCS work, so this
    # value must be ignored entirely.
    monkeypatch.setenv("POLICYENGINE_MANIFEST_DIGEST", "digest-must-be-ignored")
    sys.modules.pop("src.modal.app", None)

    source_path = Path(__file__).resolve().parents[1] / "src" / "modal" / "app.py"
    code = compile(source_path.read_text(), "/root/app.py", "exec")
    spec = importlib.util.spec_from_loader(
        "container_entrypoint_app", loader=None, origin="/root/app.py"
    )
    module = importlib.util.module_from_spec(spec)
    module.__file__ = "/root/app.py"
    exec(code, module.__dict__)

    assert module.APP_NAME.startswith("policyengine-simulation-py")
    fetch_calls = [
        call
        for call in module.simulation_image.calls
        if call[0] == "run_function" and call[1] == "fetch_artifacts"
    ]
    assert fetch_calls[0][2]["args"] == ("", None)
