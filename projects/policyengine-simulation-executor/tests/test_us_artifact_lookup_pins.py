"""Pins of the runtime lookup of prebuilt US baselines and state-level year files.

Three lookups decide whether a request reads a prebuilt file: which requests
get a fixed baseline ID (``qualifying_baseline_identity`` and
``deterministic_baseline_id``), which outcome ``ArtifactBaselineSimulation``
records when it loads one (``hit``, ``incomplete`` or ``miss``, reported as
the run's ``baseline_artifact`` attribute), and which folder a run reads its
year file from (``prebuilt_dataset_folder``, which replaced
``baked_state_data_folder``).

Which requests get a fixed ID, the outcomes and the rule a prebuilt file must
pass are what ``main`` did on 2026-11-25 (commit 544ff628), for the fixed
inputs in ``fixtures.us_prebuilt_inputs``, and must not change. The approved
1.6 · 1 design (section 2) replaced the ``bl1`` IDs with ``bl3`` ones and moved
image files to ``prebuilt/``, so the IDs, store paths and folders were
recorded from the new code.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
from fixtures.us_prebuilt_inputs import (
    enable_fixed_spm_capability,
    install_us_prebuilt_inputs,
)
from policyengine.core.scoping_strategy import RegionGroupStrategy, RowFilterStrategy
from policyengine_simulation_contract.spm import SPMInputError
from policyengine_simulation_executor import baseline_artifacts, precompute
from policyengine_simulation_executor import simulation_runtime as runtime
from policyengine_simulation_executor.region_resolution import RegionResolution
from policyengine_simulation_executor.simulation_runtime import DatasetSelection
from policyengine_simulation_executor.stage12_cache import levels, levels_us

US_NATIONAL_2026_ID = "bl3-48380a609a4e26de"
US_CALIFORNIA_GROUP_2026_ID = "bl3-ef3ab6b4dce1d62a"
DEFAULT_DATASET = DatasetSelection(
    "populace_cps", "hf://org/repo/populace_cps.h5@rev-abc", is_default=True
)
STATE_LEVEL_DATASET = DatasetSelection(
    "populace_cps_local",
    "hf://org/repo/populace_cps_local.h5@rev-local",
    is_default=False,
)
UK_DEFAULT_DATASET = DatasetSelection(
    "enhanced_frs", "hf://org/uk-repo/enhanced_frs.h5@rev-uk", is_default=True
)


@pytest.fixture
def us_inputs(monkeypatch):
    return install_us_prebuilt_inputs(monkeypatch)


def _identity_request(**overrides):
    request_facts = {
        "params": {"country": "us", "scope": "macro", "time_period": 2026},
        "country": "us",
        "dataset": DEFAULT_DATASET,
        "policy": None,
        "region_code": "us",
        "scoping_strategy": None,
        "year": 2026,
    }
    request_facts.update(overrides)
    return baseline_artifacts.BaselineIdentityRequest(**request_facts)


def _region_group_request(group, year=2026):
    params = {
        "country": "us",
        "scope": "macro",
        "region_group": list(group),
        "time_period": year,
    }
    resolution = runtime.resolve_region(
        country_module=runtime._country_module("us"), country="us", params=params
    )
    return _identity_request(
        params=params,
        region_code=resolution.code,
        scoping_strategy=resolution.scoping_strategy,
        year=year,
    )


@pytest.mark.usefixtures("us_inputs")
class TestRequestsWithAFixedBaselineId:
    @pytest.mark.parametrize(
        ("year", "expected_id", "expected_path"),
        [
            (
                2026,
                US_NATIONAL_2026_ID,
                "baselines/us/48380a609a4e26de1ed026e587f1feb69797c06f35de82e024d3c8d10f9bac1e/bl3-48380a609a4e26de.h5",
            ),
            (
                2027,
                "bl3-9f55154c42502ec9",
                "baselines/us/9f55154c42502ec96b358b6ae0a0a5b44d3bc66a84c3bc151dad17dd764993d9/bl3-9f55154c42502ec9.h5",
            ),
            (
                2025,
                "bl3-81070e29653b2b5e",
                "baselines/us/81070e29653b2b5e8ef8c6f2f3c1b09b085e5df12fddfc60d7c4adf73744a572/bl3-81070e29653b2b5e.h5",
            ),
        ],
        ids=["2026", "2027", "2025"],
    )
    def test_us_national_current_law(self, year, expected_id, expected_path):
        params = {"country": "us", "scope": "macro", "time_period": year}
        request = _identity_request(params=params, year=year)

        identity = baseline_artifacts.qualifying_baseline_identity(request)

        assert identity.region == "national"
        assert identity.scope_key is None
        assert identity.simulation_id == expected_id
        assert identity.store_path == expected_path
        assert baseline_artifacts.deterministic_baseline_id(request) == expected_id

    def test_us_region_group_matches_the_planned_baseline(self):
        request = _region_group_request(["state/ca"])

        identity = baseline_artifacts.qualifying_baseline_identity(request)

        assert identity.region == "region_group/state/ca"
        assert identity.scope_key == "region_group:row_filter:state_fips=6"
        assert identity.simulation_id == US_CALIFORNIA_GROUP_2026_ID
        california = levels.handler_for(
            "us", levels.DatasetLevel.STATE_GROUP
        ).baseline_targets(2026)[15]
        planned = precompute.baseline_target_identity(california)
        assert planned.simulation_id == US_CALIFORNIA_GROUP_2026_ID
        assert planned.store_path == identity.store_path

    def test_uk_national_current_law(self):
        request = _identity_request(
            params={"country": "uk", "scope": "macro"},
            country="uk",
            region_code="uk",
            dataset=UK_DEFAULT_DATASET,
        )

        identity = baseline_artifacts.qualifying_baseline_identity(request)

        assert identity.simulation_id == "bl3-83544ee09deea7e0"
        assert identity.store_path == (
            "baselines/uk/83544ee09deea7e005e67260e709d95791ae00eff91e1ac4297205b675f1a98b/bl3-83544ee09deea7e0.h5"
        )
        assert identity.year_file.store_path == (
            "year-files/uk/a911bc00e6d82eb65c2e72c65c3a01732f4b7b79d84ada654700445ff5c3cd0e/enhanced_frs_year_2026.h5"
        )

    def test_uk_group_of_nations_current_law(self):
        """As on main: a UK region group on the default dataset keeps a fixed ID.

        It reads the UK national year file, keyed by the group's scope.
        """
        group = RegionGroupStrategy(
            members=[
                RowFilterStrategy(variable_name="region", variable_value="ENGLAND"),
                RowFilterStrategy(variable_name="region", variable_value="WALES"),
            ]
        )
        request = _identity_request(
            params={"country": "uk", "scope": "macro"},
            country="uk",
            region_code="region_group/country/england+country/wales",
            scoping_strategy=group,
            dataset=UK_DEFAULT_DATASET,
        )

        identity = baseline_artifacts.qualifying_baseline_identity(request)

        assert identity.region == "region_group/country/england+country/wales"
        assert identity.scope_key == group.cache_key
        assert identity.year_file.store_path.endswith("/enhanced_frs_year_2026.h5")
        assert identity.year_file.subset is None
        assert baseline_artifacts.deterministic_baseline_id(request) == (
            identity.simulation_id
        )

    def test_us_national_with_the_spm_capability(self, monkeypatch, us_inputs):
        enable_fixed_spm_capability(monkeypatch, us_inputs)

        identity = baseline_artifacts.qualifying_baseline_identity(_identity_request())

        assert identity.simulation_id == "bl3-a19ba492ccb50823"
        assert identity.storage_id == (
            "bl3-a19ba492ccb50823-spm-7396bf5f4876c42bb6cba0f9533478098edc0861cc3657bd0d60f88dbb26ac39"
        )
        assert identity.store_path == (
            "baselines/us/a19ba492ccb50823fe231bc62a5da265a82a53bc3907de616f3334eb82ca20e8/"
            "bl3-a19ba492ccb50823-spm-7396bf5f4876c42bb6cba0f9533478098edc0861cc3657bd0d60f88dbb26ac39.h5"
        )

    @pytest.mark.parametrize(
        "overrides",
        [
            pytest.param({"policy": {"gov.irs.credits.ctc.amount": 1}}, id="reform"),
            pytest.param(
                {"params": {"country": "us", "scope": "household"}}, id="household"
            ),
            pytest.param({"params": {"country": "us"}}, id="no-scope"),
            pytest.param({"dataset": STATE_LEVEL_DATASET}, id="state-level-dataset"),
            pytest.param({"region_code": None}, id="unresolved-region"),
            pytest.param({"region_code": "state/ca"}, id="unscoped-state"),
            pytest.param(
                {
                    "region_code": "state/ca",
                    "scoping_strategy": RowFilterStrategy(
                        variable_name="state_fips", variable_value=6
                    ),
                },
                id="single-state",
            ),
            pytest.param(
                {
                    "country": "uk",
                    "region_code": "uk",
                    "dataset": DatasetSelection(
                        "dense", "hf://dense", is_default=False
                    ),
                },
                id="uk-local-area",
            ),
        ],
    )
    def test_requests_without_a_fixed_id(self, overrides):
        request = _identity_request(**overrides)

        assert baseline_artifacts.qualifying_baseline_identity(request) is None
        assert baseline_artifacts.deterministic_baseline_id(request) is None

    def test_identity_collection_failure(self, monkeypatch):
        from policyengine_simulation_executor import artifact_keys

        def failing_collection(*_arguments, **_keyword_arguments):
            msg = "release manifest unavailable"
            raise RuntimeError(msg)

        monkeypatch.setattr(artifact_keys, "year_file_identity", failing_collection)

        with pytest.raises(RuntimeError, match="release manifest unavailable"):
            baseline_artifacts.qualifying_baseline_identity(_identity_request())
        assert baseline_artifacts.deterministic_baseline_id(_identity_request()) is None

    def test_an_spm_selection_without_a_capability_is_refused(self):
        params = {
            "country": "us",
            "scope": "macro",
            "time_period": 2026,
            "spm": {"scenario": "ce_trend"},
        }

        with pytest.raises(SPMInputError):
            baseline_artifacts.deterministic_baseline_id(
                _identity_request(params=params)
            )


class _LoadingModelVersion:
    """A model version whose ``load`` finds a complete, incomplete or no artifact."""

    required_columns = ("age", "income")

    def __init__(self, stored_artifact: str) -> None:
        self.stored_artifact = stored_artifact
        self.calls: list[str] = []

    def resolve_entity_variables(self, _simulation):
        return {"person": list(self.required_columns)}

    def load(self, simulation):
        self.calls.append("load")
        if self.stored_artifact == "absent":
            msg = "no artifact"
            raise FileNotFoundError(msg)
        if self.stored_artifact == "unreadable":
            msg = "corrupt h5"
            raise OSError(msg)
        columns = (
            self.required_columns
            if self.stored_artifact == "complete"
            else self.required_columns[:1]
        )
        simulation.output_dataset = _output_with_columns(columns)

    def run(self, simulation):
        self.calls.append("run")
        simulation.output_dataset = _output_with_columns(self.required_columns)

    def save(self, _simulation):
        self.calls.append("save")


def _output_with_columns(columns):
    frame = pd.DataFrame({column: [1.0] for column in columns})
    return SimpleNamespace(data=SimpleNamespace(entity_data={"person": frame}))


@pytest.fixture
def empty_simulation_cache(monkeypatch):
    from policyengine.core import simulation as simulation_module
    from policyengine.core.cache import LRUCache

    monkeypatch.setattr(simulation_module, "_cache", LRUCache(max_size=10))


@pytest.mark.usefixtures("empty_simulation_cache")
class TestBaselineArtifactOutcomes:
    @pytest.mark.parametrize(
        ("stored_artifact", "expected_outcome", "expected_calls"),
        [
            ("complete", "hit", ["load"]),
            ("incomplete", "incomplete", ["load", "run", "save"]),
            ("absent", "miss", ["load", "run", "save"]),
            ("unreadable", "miss", ["load", "run", "save"]),
        ],
    )
    def test_outcome_of_the_first_ensure(
        self, stored_artifact, expected_outcome, expected_calls
    ):
        model_version = _LoadingModelVersion(stored_artifact)
        simulation = baseline_artifacts.ArtifactBaselineSimulation.model_construct(
            id=US_NATIONAL_2026_ID,
            dataset=None,
            tax_benefit_model_version=model_version,
            policy=None,
            dynamic=None,
            scoping_strategy=None,
            extra_variables={},
            output_dataset=None,
        )

        simulation.ensure()
        simulation.ensure()

        assert simulation.artifact_outcome == expected_outcome
        assert model_version.calls == expected_calls

    def test_outcome_labels(self):
        assert (
            baseline_artifacts.OUTCOME_HIT,
            baseline_artifacts.OUTCOME_INCOMPLETE,
            baseline_artifacts.OUTCOME_MISS,
        ) == ("hit", "incomplete", "miss")


@pytest.fixture
def baked_utah_folder(us_inputs, tmp_path, monkeypatch):
    """Return a data folder holding Utah's 2026 state-level year file.

    The file passes the reuse checks until a test sets ``reuse_checks.problem``.
    """
    folder = tmp_path / "prebuilt" / "us" / "populace_cps_local" / "UT"
    folder.mkdir(parents=True)
    (folder / "populace_cps_local_year_2026.h5").write_bytes(b"year file")
    reuse_checks = SimpleNamespace(problem=None, checked=[])

    def year_file_reuse_problem(path, dataset):
        reuse_checks.checked.append((Path(path), dataset))
        if isinstance(reuse_checks.problem, Exception):
            raise reuse_checks.problem
        return reuse_checks.problem

    monkeypatch.setattr(levels_us, "year_file_reuse_problem", year_file_reuse_problem)
    return SimpleNamespace(
        data_folder=str(tmp_path),
        folder=folder,
        reuse_checks=reuse_checks,
        state_level=STATE_LEVEL_DATASET,
        default=DEFAULT_DATASET,
    )


def _baked_folder(baked, **overrides):
    lookup = {
        "country": "us",
        "selection": baked.state_level,
        "state_code": "UT",
        "year": 2026,
    }
    lookup.update(overrides)
    state_code = lookup["state_code"]
    region = RegionResolution(
        code=f"state/{(state_code or 'xx').lower()}",
        scoping_strategy=RowFilterStrategy(
            variable_name="state_fips",
            variable_value={"UT": 49, "ut": 49, "WY": 56}.get(state_code, 0),
        ),
        state_code=state_code,
    )
    found = levels.prebuilt_dataset_folder(
        levels.RunScope(
            country=lookup["country"],
            year=lookup["year"],
            region=region,
            dataset=lookup["selection"],
        ),
        baked.data_folder,
    )
    return None if found is None else found.folder


class TestBakedStateDataFolder:
    def test_a_state_reads_its_baked_folder(self, baked_utah_folder):
        assert _baked_folder(baked_utah_folder) == str(baked_utah_folder.folder)
        assert baked_utah_folder.reuse_checks.checked == [
            (
                baked_utah_folder.folder / "populace_cps_local_year_2026.h5",
                "populace_cps_local",
            )
        ]

    def test_a_lower_case_state_code_reads_the_same_folder(self, baked_utah_folder):
        assert _baked_folder(baked_utah_folder, state_code="ut") == str(
            baked_utah_folder.folder
        )

    @pytest.mark.parametrize(
        "overrides",
        [
            pytest.param({"country": "uk"}, id="uk"),
            pytest.param({"state_code": None}, id="no-state"),
            pytest.param({"state_code": "WY"}, id="state-without-a-file"),
            pytest.param({"year": 2027}, id="year-without-a-file"),
        ],
    )
    def test_requests_that_read_nothing_baked(self, baked_utah_folder, overrides):
        assert _baked_folder(baked_utah_folder, **overrides) is None

    def test_the_default_dataset_reads_nothing_baked(self, baked_utah_folder):
        assert (
            _baked_folder(baked_utah_folder, selection=baked_utah_folder.default)
            is None
        )

    def test_a_file_that_fails_the_reuse_checks_reads_nothing_baked(
        self, baked_utah_folder
    ):
        baked_utah_folder.reuse_checks.problem = "year-file stamp is missing"

        assert _baked_folder(baked_utah_folder) is None

    def test_unimportable_reuse_checks_read_nothing_baked(self, baked_utah_folder):
        baked_utah_folder.reuse_checks.problem = ImportError("moved helper")

        assert _baked_folder(baked_utah_folder) is None
