"""Precompute app: function shapes, planning logic, writer==reader pins.

The app itself runs only under `modal run`; these tests cover everything
around the remote calls — the image/function declarations (fake modal, as
in test_modal_bundle_image), the pure work-selection and manifest assembly,
and the contract that the writer derives identical identities to the
runtime reader through the same executor code path.
"""

import importlib
import sys
from types import ModuleType, SimpleNamespace

import pytest


class FakeImage:
    def __init__(self):
        self.calls = []

    @classmethod
    def debian_slim(cls, **kwargs):
        image = cls()
        image.calls.append(("debian_slim", kwargs))
        return image

    def _record(self, name):
        def method(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return self

        return method

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return self._record(name)


class FakeSecret:
    @staticmethod
    def from_name(name, **kwargs):
        return {"name": name, **kwargs}


class FakeApp:
    instances = []

    def __init__(self, name):
        self.name = name
        self.function_calls = []
        FakeApp.instances.append(self)

    def function(self, **kwargs):
        def decorator(fn):
            self.function_calls.append((fn.__name__, kwargs))
            return fn

        return decorator

    def local_entrypoint(self, **kwargs):
        def decorator(fn):
            return fn

        return decorator


@pytest.fixture
def precompute_module(monkeypatch):
    fake_modal = ModuleType("modal")
    fake_modal.Image = FakeImage
    fake_modal.Secret = FakeSecret
    fake_modal.App = FakeApp
    fake_modal.is_local = lambda: True
    fake_modal.asgi_app = lambda **kwargs: lambda fn: fn
    monkeypatch.setitem(sys.modules, "modal", fake_modal)
    for env in (
        "POLICYENGINE_VERSION",
        "POLICYENGINE_CORE_VERSION",
        "POLICYENGINE_US_VERSION",
        "POLICYENGINE_UK_VERSION",
    ):
        monkeypatch.setenv(env, "0.0.0-test")
    FakeApp.instances = []
    for module in ("src.modal.app", "src.modal.precompute_app"):
        sys.modules.pop(module, None)
    module = importlib.import_module("src.modal.precompute_app")
    yield module
    for name in ("src.modal.app", "src.modal.precompute_app"):
        sys.modules.pop(name, None)


def _function_kwargs(module, name):
    app = next(
        instance
        for instance in FakeApp.instances
        if instance.name == "policyengine-simulation-precompute"
    )
    return dict(app.function_calls)[name]


class TestAppShape:
    def test_all_worker_functions_carry_gcp_secret(self, precompute_module):
        for name in (
            "plan_artifacts",
            "build_dataset",
            "compute_baseline",
            "verify_determinism",
            "publish_manifest",
        ):
            secrets = _function_kwargs(precompute_module, name)["secrets"]
            assert {"name": "gcp-credentials", "environment_name": "main"} in secrets

    def test_compute_baseline_matches_segment_worker_shape(self, precompute_module):
        kwargs = _function_kwargs(precompute_module, "compute_baseline")
        assert kwargs["cpu"] == 8.0
        assert kwargs["memory"] == 32768
        assert kwargs["timeout"] == 3600

    def test_dataset_builder_gets_prebuild_scale_resources(self, precompute_module):
        kwargs = _function_kwargs(precompute_module, "build_dataset")
        assert kwargs["cpu"] == 8.0
        assert kwargs["memory"] == 65536

    def test_image_includes_local_source(self, precompute_module):
        calls = precompute_module.precompute_image.calls
        local_source = [call for call in calls if call[0] == "add_local_python_source"]
        assert local_source, "precompute image must mount executor source"
        assert set(local_source[-1][1]) == {
            "src.modal",
            "policyengine_simulation_executor",
            "policyengine_simulation_observability",
            "policyengine_simulation_contract",
        }

    def test_years_are_in_priority_order(self, precompute_module):
        assert precompute_module.PRECOMPUTE_YEARS == [2026, 2027, 2025]


def _plan():
    return {
        "datasets": [
            {
                "year": 2026,
                "digest": "d1",
                "path": "datasets/us/d1/populace_year_2026.h5",
                "filename": "populace_year_2026.h5",
                "exists": True,
            },
            {
                "year": 2027,
                "digest": "d2",
                "path": "datasets/us/d2/populace_year_2027.h5",
                "filename": "populace_year_2027.h5",
                "exists": False,
            },
        ],
        "baselines": [
            {
                "year": 2026,
                "group": ["state/ca"],
                "region": "region_group/state/ca",
                "digest": "b1",
                "path": "baselines/us/b1/bl1-aaaa.h5",
                "simulation_id": "bl1-aaaa",
                "exists": True,
            },
            {
                "year": 2027,
                "group": ["state/ca"],
                "region": "region_group/state/ca",
                "digest": "b2",
                "path": "baselines/us/b2/bl1-bbbb.h5",
                "simulation_id": "bl1-bbbb",
                "exists": False,
            },
        ],
        "receipt": {"policyengine_version": "4.22.0", "model_version": "1.0.0"},
    }


class TestPlanning:
    def test_select_work_computes_only_misses(self, precompute_module):
        work = precompute_module.select_work(_plan(), force=False)
        assert [entry["year"] for entry in work["datasets"]] == [2027]
        assert [entry["simulation_id"] for entry in work["baselines"]] == ["bl1-bbbb"]

    def test_force_selects_everything(self, precompute_module):
        work = precompute_module.select_work(_plan(), force=True)
        assert len(work["datasets"]) == 2
        assert len(work["baselines"]) == 2

    def test_manifest_lists_every_artifact_with_runtime_filenames(
        self, precompute_module
    ):
        payload = precompute_module.build_manifest_payload(_plan())
        assert payload["schema"] == precompute_module.MANIFEST_SCHEMA
        assert payload["receipt"] == _plan()["receipt"]
        by_type = {}
        for artifact in payload["artifacts"]:
            by_type.setdefault(artifact["type"], []).append(artifact)
        assert [a["filename"] for a in by_type["dataset"]] == [
            "populace_year_2026.h5",
            "populace_year_2027.h5",
        ]
        # Baseline runtime filename is {simulation_id}.h5 — the exact name
        # Simulation.ensure() resolves beside the dataset.
        assert [a["filename"] for a in by_type["baseline"]] == [
            "bl1-aaaa.h5",
            "bl1-bbbb.h5",
        ]


class TestWriterReaderContract:
    """The single most important pins: writer identity == reader identity."""

    @pytest.fixture
    def identity_stubs(self, monkeypatch):
        from policyengine.provenance import manifest as manifest_module

        from policyengine_simulation_executor import release_bundle

        monkeypatch.setattr(
            release_bundle,
            "get_country_release_bundle",
            lambda country: SimpleNamespace(
                country="us",
                policyengine_version="4.22.0",
                model_version="9.9.9",
                data_version="1.2.3",
                data_artifact_revision="rev-abc",
                default_dataset="populace_cps",
            ),
        )
        monkeypatch.setattr(release_bundle, "_receipt_dataset", lambda country: None)
        monkeypatch.setattr(
            manifest_module,
            "resolve_dataset_reference",
            lambda country, dataset: f"{dataset}.h5",
        )
        monkeypatch.setattr(
            manifest_module, "dataset_logical_name", lambda reference: "populace_cps"
        )
        monkeypatch.setattr(
            manifest_module,
            "get_release_manifest",
            lambda country: SimpleNamespace(certification=None),
        )

    @pytest.fixture
    def fake_regions(self, monkeypatch):
        from policyengine.core.scoping_strategy import RowFilterStrategy

        from policyengine_simulation_executor import simulation_runtime as sr

        def get_region(code):
            state = code.split("/")[-1].upper()
            return SimpleNamespace(
                code=code,
                scoping_strategy=RowFilterStrategy(
                    variable_name="state_code", variable_value=state
                ),
            )

        monkeypatch.setattr(
            sr,
            "_country_module",
            lambda country: SimpleNamespace(
                model=SimpleNamespace(get_region=get_region)
            ),
        )

    def test_precompute_identity_equals_runtime_id(
        self, precompute_module, identity_stubs, fake_regions
    ):
        from policyengine_simulation_executor import simulation_runtime as sr
        from policyengine_simulation_executor.baseline_artifacts import (
            deterministic_baseline_id,
        )

        group = ["state/ca", "state/wv"]
        writer_identity = precompute_module._cohort_identity(2026, group)

        params = precompute_module._cohort_params(2026, group)
        resolution = sr._resolve_region(
            country_module=sr._country_module("us"), country="us", params=params
        )
        reader_id = deterministic_baseline_id(
            params,
            country="us",
            policy=None,
            region_code=resolution.code,
            scoping_strategy=resolution.scoping_strategy,
            year=2026,
        )
        assert writer_identity.simulation_id == reader_id
        assert writer_identity.store_path.endswith(
            f"/{writer_identity.simulation_id}.h5"
        )

    def test_dataset_filename_matches_runtime_stem_lookup(self, monkeypatch):
        """Real-manifest pin: the artifact filename equals the exact name
        the runtime's ensure_datasets existence check resolves (idiom of
        test_image_setup_prebuild)."""
        from policyengine.provenance.manifest import (
            dataset_logical_name,
            resolve_dataset_reference,
        )

        from policyengine_simulation_executor.artifact_keys import (
            collect_dataset_identity,
        )
        from policyengine_simulation_executor.release_bundle import (
            get_country_release_bundle,
            resolve_bundle_dataset_name,
        )
        from policyengine_simulation_executor.simulation_runtime import DEFAULT_YEAR

        monkeypatch.delenv("POLICYENGINE_BUNDLE_RECEIPT", raising=False)
        get_country_release_bundle.cache_clear()
        try:
            expected_stem = dataset_logical_name(
                resolve_dataset_reference("us", resolve_bundle_dataset_name("us", None))
            )
            identity = collect_dataset_identity("us", DEFAULT_YEAR)
        finally:
            get_country_release_bundle.cache_clear()
        assert identity.filename == f"{expected_stem}_year_{DEFAULT_YEAR}.h5"
