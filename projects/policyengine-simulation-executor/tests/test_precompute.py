"""Precompute library: planning, manifest assembly, orchestration, and the
writer==reader identity contract.

No Modal involved — the library takes function handles as parameters, so
the orchestration runs against plain fakes here. Everything structured is
a strict model from precompute_models; the fakes speak dicts, exactly
like Modal's serialization boundary does.
"""

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from policyengine_simulation_executor import precompute
from policyengine_simulation_executor.artifact_keys import canonical_digest
from policyengine_simulation_executor.precompute_models import (
    ArtifactManifest,
    BaselinePlanEntry,
    BundleReceipt,
    DatasetPlanEntry,
    PrecomputePlan,
)


def _receipt() -> BundleReceipt:
    return BundleReceipt(
        policyengine_version="4.22.0",
        model_version="1.0.0",
        data_version="1.2.3",
        data_artifact_revision="rev-abc",
        default_dataset="populace_cps",
    )


def _plan() -> PrecomputePlan:
    return PrecomputePlan(
        datasets=[
            DatasetPlanEntry(
                year=2026,
                digest="d1",
                path="datasets/us/d1/populace_year_2026.h5",
                filename="populace_year_2026.h5",
                exists=True,
            ),
            DatasetPlanEntry(
                year=2027,
                digest="d2",
                path="datasets/us/d2/populace_year_2027.h5",
                filename="populace_year_2027.h5",
                exists=False,
            ),
        ],
        baselines=[
            BaselinePlanEntry(
                year=2026,
                group=["state/ca"],
                region="region_group/state/ca",
                digest="b1",
                path="baselines/us/b1/bl1-aaaa.h5",
                simulation_id="bl1-aaaa",
                exists=True,
            ),
            BaselinePlanEntry(
                year=2027,
                group=["state/ca"],
                region="region_group/state/ca",
                digest="b2",
                path="baselines/us/b2/bl1-bbbb.h5",
                simulation_id="bl1-bbbb",
                exists=False,
            ),
        ],
        receipt=_receipt(),
    )


class TestSchemas:
    def test_plan_round_trips_through_the_modal_boundary(self):
        plan = _plan()
        assert PrecomputePlan.model_validate(plan.model_dump()) == plan

    def test_unknown_keys_are_rejected(self):
        payload = _plan().model_dump()
        payload["surprise"] = True
        with pytest.raises(ValidationError):
            PrecomputePlan.model_validate(payload)

    def test_missing_keys_are_rejected(self):
        payload = _plan().model_dump()
        del payload["baselines"][0]["simulation_id"]
        with pytest.raises(ValidationError):
            PrecomputePlan.model_validate(payload)


class TestPlanning:
    def test_select_work_computes_only_misses(self):
        work = precompute.select_work(_plan(), force=False)
        assert [entry.year for entry in work.datasets] == [2027]
        assert [entry.simulation_id for entry in work.baselines] == ["bl1-bbbb"]

    def test_force_selects_everything(self):
        work = precompute.select_work(_plan(), force=True)
        assert len(work.datasets) == 2
        assert len(work.baselines) == 2

    def test_years_are_in_priority_order(self):
        assert precompute.PRECOMPUTE_YEARS == [2026, 2027, 2025]

    def test_manifest_lists_every_artifact_with_runtime_filenames(self):
        manifest = precompute.build_manifest(_plan())
        assert manifest.manifest_schema == precompute.MANIFEST_SCHEMA
        assert manifest.receipt == _receipt()
        by_type: dict[str, list] = {}
        for artifact in manifest.artifacts:
            by_type.setdefault(artifact.type, []).append(artifact)
        assert [a.filename for a in by_type["dataset"]] == [
            "populace_year_2026.h5",
            "populace_year_2027.h5",
        ]
        # Baseline runtime filename is {simulation_id}.h5 — the exact name
        # Simulation.ensure() resolves beside the dataset.
        assert [a.filename for a in by_type["baseline"]] == [
            "bl1-aaaa.h5",
            "bl1-bbbb.h5",
        ]

    def test_manifest_payload_is_wire_compatible(self):
        """The manifest is content-addressed: the typed model must digest
        identically to the raw dict shape already published to the store
        (key names included — note "schema", not "manifest_schema")."""
        raw = {
            "schema": "mf1",
            "country": "us",
            "receipt": {
                "policyengine_version": "4.22.0",
                "model_version": "1.0.0",
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
                {
                    "type": "dataset",
                    "path": "datasets/us/d2/populace_year_2027.h5",
                    "filename": "populace_year_2027.h5",
                    "year": 2027,
                    "digest": "d2",
                },
                {
                    "type": "baseline",
                    "path": "baselines/us/b1/bl1-aaaa.h5",
                    "filename": "bl1-aaaa.h5",
                    "year": 2026,
                    "digest": "b1",
                },
                {
                    "type": "baseline",
                    "path": "baselines/us/b2/bl1-bbbb.h5",
                    "filename": "bl1-bbbb.h5",
                    "year": 2027,
                    "digest": "b2",
                },
            ],
        }
        payload = precompute.build_manifest(_plan()).canonical_payload()
        assert payload == raw
        assert canonical_digest(payload) == canonical_digest(raw)
        # And the wire shape validates back into the model.
        assert ArtifactManifest.model_validate(raw).canonical_payload() == raw


class FakeHandle:
    def __init__(self, result):
        self._result = result

    def get(self):
        return self._result


class FakeFunction:
    """Stands in for a Modal function handle (.remote / .spawn).

    Speaks dicts only, as the real boundary does.
    """

    def __init__(self, result=None, results_by_year=None):
        self.result = result
        self.results_by_year = results_by_year or {}
        self.remote_calls = []
        self.spawn_calls = []

    def remote(self, *args):
        self.remote_calls.append(args)
        return self.result

    def spawn(self, *args):
        self.spawn_calls.append(args)
        year = args[1]["year"] if len(args) > 1 else None
        return FakeHandle(self.results_by_year.get(year, self.result))


def _dataset_result(year):
    return {
        "year": year,
        "path": f"datasets/us/d/populace_year_{year}.h5",
        "uploaded": True,
        "build_seconds": 1.0,
        "size_bytes": 10,
    }


def _baseline_result(year):
    return {
        "year": year,
        "group": ["state/ca"],
        "simulation_id": f"bl1-{year}",
        "outcome": "miss",
        "uploaded": True,
        "compute_seconds": 2.0,
        "size_bytes": 10,
    }


class TestRunPrecompute:
    def _functions(self, verify_result=None):
        return {
            "plan_artifacts": FakeFunction(result=_plan().model_dump()),
            "build_dataset": FakeFunction(
                results_by_year={2027: _dataset_result(2027)}
            ),
            "compute_baseline": FakeFunction(
                results_by_year={2027: _baseline_result(2027)}
            ),
            "verify_determinism": FakeFunction(
                result=verify_result or {"equal": True, "differences": []}
            ),
            "publish_manifest": FakeFunction(result="digest-123"),
        }

    def _run(self, functions, *, force=False):
        lines = []
        digest = precompute.run_precompute(
            "bucket-x", force=force, echo=lines.append, **functions
        )
        return digest, lines

    def test_spawns_only_misses_and_gates_on_first_computed(self):
        functions = self._functions()
        digest, lines = self._run(functions)

        (dataset_spawn,) = functions["build_dataset"].spawn_calls
        assert dataset_spawn[0] == "bucket-x"
        assert dataset_spawn[1] == _plan().datasets[1].model_dump()
        (baseline_spawn,) = functions["compute_baseline"].spawn_calls
        assert baseline_spawn[1] == _plan().baselines[1].model_dump()
        # The gate probes the first computed baseline, with its plan entry.
        (verify_call,) = functions["verify_determinism"].remote_calls
        assert verify_call[1]["simulation_id"] == "bl1-bbbb"
        assert digest == "digest-123"

    def test_last_echo_line_is_the_digest_contract(self):
        _, lines = self._run(self._functions())
        assert lines[-1] == "MANIFEST_DIGEST=digest-123"
        assert lines[-1].startswith(precompute.MANIFEST_DIGEST_PREFIX)

    def test_noop_run_spawns_nothing_and_skips_the_gate(self):
        functions = self._functions()
        noop_plan = _plan()
        for entry in noop_plan.datasets + noop_plan.baselines:
            entry.exists = True
        functions["plan_artifacts"] = FakeFunction(result=noop_plan.model_dump())

        digest, lines = self._run(functions)
        assert functions["build_dataset"].spawn_calls == []
        assert functions["compute_baseline"].spawn_calls == []
        assert functions["verify_determinism"].remote_calls == []
        # The manifest still publishes: a no-op run must refresh nothing
        # but the deploy still needs a digest for the fetch layer.
        assert digest == "digest-123"
        assert lines[-1] == "MANIFEST_DIGEST=digest-123"

    def test_failed_gate_aborts_before_publishing(self):
        functions = self._functions(
            verify_result={"equal": False, "differences": ["person.age: values differ"]}
        )
        with pytest.raises(SystemExit, match="values differ"):
            self._run(functions)
        assert functions["publish_manifest"].remote_calls == []

    def test_malformed_worker_result_fails_loudly(self):
        functions = self._functions()
        functions["build_dataset"] = FakeFunction(
            results_by_year={2027: {"year": 2027, "unexpected": "shape"}}
        )
        with pytest.raises(ValidationError):
            self._run(functions)


class TestWriterReaderContract:
    """The writer and the runtime reader must derive identical identities."""

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

    def test_precompute_identity_equals_runtime_id(self, identity_stubs, fake_regions):
        from policyengine_simulation_executor import simulation_runtime as sr
        from policyengine_simulation_executor.baseline_artifacts import (
            deterministic_baseline_id,
        )

        group = ["state/ca", "state/wv"]
        writer_identity = precompute.cohort_identity(2026, group)

        params = precompute.cohort_params(2026, group)
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
        """Real-manifest coverage: the artifact filename equals the exact
        name the runtime's ensure_datasets existence check resolves."""
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
