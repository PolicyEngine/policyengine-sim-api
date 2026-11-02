"""Tests for UK runs on a bundle that routes area regions, and local-area breakdowns."""

from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from fixtures.macro_output import CURRENT_SINGLE_YEAR_MACRO_RESULT
from fixtures.weighted_frames import weighted_entity_frame
from policyengine.core.scoping_strategy import RowFilterStrategy
from policyengine_simulation_contract.job_progress import JobStage
from policyengine_simulation_contract.uk_geography import (
    UKLocalAuthorityBoundaryVersion,
    UKLocalAuthorityMetadata,
)
from policyengine_simulation_executor import simulation_output_geographic
from policyengine_simulation_executor.release_bundle import get_country_release_bundle
from policyengine_simulation_executor.simulation_macro_output import (
    GeographicImpactOutput,
)
from policyengine_simulation_executor.simulation_output_local_areas import (
    LocalAreaBreakdownBuilder,
)
from policyengine_simulation_executor.simulation_runtime import (
    DatasetSelection,
    RegionResolution,
    _run_simulation_impl_core,
    normalise_policy,
    resolve_region,
)
from policyengine_simulation_observability.stages import ANNUAL_IMPACT_STAGES, Stage
from test_simulation_output_fakes import TrackingRuntime, constituency_impact_record


def test_run_simulation_impl_core_passes_detected_uk_boundary_to_builder(monkeypatch):
    # A bundle that does not route area regions: the national file carries
    # the area codes and the run builds the area breakdowns from them.
    _routes_uk_area_regions(monkeypatch, routed=False)
    dataset = SimpleNamespace(
        data=SimpleNamespace(
            entity_data={
                "household": weighted_entity_frame(
                    {"la_code_oa": ["E06000001", "E07000026"]}
                )
            }
        )
    )
    country_module = SimpleNamespace(model=SimpleNamespace(version="2.90.0"))
    builder_calls: list[dict] = []

    class FakeSimulationOutputBuilder:
        def __init__(self, **kwargs):
            builder_calls.append(kwargs)

        def serialize(self):
            return dict(CURRENT_SINGLE_YEAR_MACRO_RESULT)

    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_runtime._country_module",
        lambda _country: country_module,
    )
    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_runtime.resolve_region",
        lambda **_kwargs: RegionResolution(code="uk", dataset_reference=None),
    )
    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_runtime._load_dataset",
        lambda _params, country_module, selection, data_folder=None: dataset,
    )
    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_runtime._build_simulation",
        lambda *_args, **_kwargs: SimpleNamespace(
            on_first_ensure=lambda _callback: None
        ),
    )
    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_runtime.SimulationOutputBuilder",
        FakeSimulationOutputBuilder,
    )

    _run_simulation_impl_core(
        {"country": "uk", "baseline": {}, "reform": {}},
        runtime=TrackingRuntime(),
    )

    metadata = builder_calls[0]["uk_local_authority_metadata"]
    assert metadata == UKLocalAuthorityMetadata(
        boundary_version=UKLocalAuthorityBoundaryVersion.LAD22
    )
    assert builder_calls[0]["omit_uk_area_breakdowns"] is False


def _routes_uk_area_regions(monkeypatch, *, routed: bool) -> None:
    monkeypatch.setattr(
        "policyengine_simulation_executor.uk_local_authority_metadata."
        "uk_area_regions_routed",
        lambda: routed,
    )


def _uk_dataset(**household: list[object]) -> SimpleNamespace:
    return SimpleNamespace(
        data=SimpleNamespace(
            entity_data={"household": weighted_entity_frame(household)}
        )
    )


_NATIONAL_SELECTION = DatasetSelection(
    "microcosm_uk_2024_25", "hf://policyengine/uk/national.h5@cut", is_default=True
)
_LOCAL_AREA_SELECTION = DatasetSelection(
    "microcosm_uk_2024_25_dense", "hf://policyengine/uk/local.h5@cut", is_default=False
)


_LOCAL_AREA_REVISION = "microcosm-uk-2024-25-dense-20261019T000432Z-ee5c2dbe"
_WALES = RowFilterStrategy(variable_name="region", variable_value="WALES")
_TEST_POLICIES = {
    "baseline": {"gov.test.parameter": {"2026-01-01.2100-12-31": 1}},
    "reform": {"gov.test.parameter": {"2026-01-01.2100-12-31": 2}},
}
_WALES_NATIONAL_PARAMS = {"country": "uk", "region": "country/wales", **_TEST_POLICIES}
_WALES_BREAKDOWN_PARAMS = {
    "country": "uk",
    "scope": "macro",
    "region": "country/wales",
    "time_period": "2026",
    # The Public API always sends include_cliffs: false.
    "include_cliffs": False,
    "breakdown": "local_areas",
    **_TEST_POLICIES,
}


def _routed_area_dataset(*, without: str | None = None) -> SimpleNamespace:
    """Return a local-area dataset with every column its area outputs read.

    ``without`` names one column to leave out.
    """
    household: dict[str, list[object]] = {
        "region": ["WALES", "WALES"],
        "constituency_code_oa": ["E14001063", "W07000112"],
        "la_code_oa": ["E06000063", "W06000001"],
        "region_code_oa": ["WALES", "WALES"],
        "source_household_id": [1, 2],
    }
    household.pop(without or "", None)
    return _uk_dataset(**household)


@dataclass
class _UKAreaRun:
    """A UK run through ``_run_simulation_impl_core`` with every seam faked."""

    builds: list[dict]
    builder_calls: list[dict]
    runtime: TrackingRuntime
    # Each selection _load_dataset was asked for, and each country module load.
    loads: list[DatasetSelection]
    country_modules: list[str]
    # The builder methods the run called to produce its output, and the output.
    serialized: list[str]
    output: object = None

    def dataset_load_attributes(self) -> list[dict]:
        return [
            node["attrs"]
            for node in self.runtime.roots
            if node["name"] == ANNUAL_IMPACT_STAGES.name(Stage.DATASET_LOAD)
        ]


class _RoutedUKSeams:
    """Fakes for each seam of a routed UK run, recording into one ``_UKAreaRun``."""

    def __init__(self, run, *, local_area_dataset):
        self.run = run
        self.local_area_dataset = local_area_dataset
        self.national_dataset = _uk_dataset(region=["WALES", "WALES"])

    def country_module(self, country):
        self.run.country_modules.append(country)
        return SimpleNamespace(model=SimpleNamespace(version="2.132.6"))

    def resolve_region(self, *, params, **_kwargs):
        region = params.get("region")
        if region == "country/wales":
            return RegionResolution(
                code="country/wales", dataset_reference=None, scoping_strategy=_WALES
            )
        region_type = str(region).partition("/")[0]
        if region_type in {"constituency", "local_authority"}:
            # An area region names the routed local-area dataset itself.
            return RegionResolution(
                code=region,
                dataset_reference=_LOCAL_AREA_SELECTION.name,
                scoping_strategy=RowFilterStrategy(
                    variable_name=f"{region_type}_code_oa",
                    variable_value=region.partition("/")[2],
                ),
            )
        return RegionResolution(code="uk", dataset_reference=None)

    def resolve_selection(self, _params, *, region_resolution):
        if region_resolution.dataset_reference == _LOCAL_AREA_SELECTION.name:
            return _LOCAL_AREA_SELECTION
        return _NATIONAL_SELECTION

    def load_dataset(self, _params, *, selection, country_module, data_folder=None):
        self.run.loads.append(selection)
        if selection is _LOCAL_AREA_SELECTION:
            return self.local_area_dataset
        return self.national_dataset

    def build_simulation(self, _params, *, dataset, dataset_selection, policy, region):
        simulation = SimpleNamespace(on_first_ensure=lambda _callback: None)
        self.run.builds.append(
            {
                "dataset": dataset,
                "selection": dataset_selection,
                "policy": policy,
                "scoping_strategy": region.scoping_strategy,
                "simulation": simulation,
            }
        )
        return simulation

    def output_builder(self, **kwargs):
        self.run.builder_calls.append(kwargs)
        return _RecordingBuilder(
            self.run, "macro", dict(CURRENT_SINGLE_YEAR_MACRO_RESULT)
        )

    def breakdown_builder(self, **kwargs):
        self.run.builder_calls.append(kwargs)
        return _RecordingBuilder(
            self.run, "local_area_breakdown", {"kind": "local_areas"}
        )


class _RecordingBuilder:
    """Records which builder serialized the run's output."""

    def __init__(self, run, kind, output):
        self.run = run
        self.kind = kind
        self.output = output

    def serialize(self):
        self.run.serialized.append(self.kind)
        return self.output


def _install_routed_uk_fakes(
    monkeypatch, *, local_area_dataset, routed: bool = True
) -> _UKAreaRun:
    """Fake every seam of a UK run whose national file carries no area codes."""
    run = _UKAreaRun(
        builds=[],
        builder_calls=[],
        runtime=TrackingRuntime(),
        loads=[],
        country_modules=[],
        serialized=[],
    )
    seams = _RoutedUKSeams(run, local_area_dataset=local_area_dataset)
    _routes_uk_area_regions(monkeypatch, routed=routed)
    monkeypatch.setattr(
        "policyengine_simulation_executor.uk_local_authority_metadata."
        "routed_uk_area_dataset_name",
        lambda: _LOCAL_AREA_SELECTION.name,
    )
    for name, value in {
        "_country_module": seams.country_module,
        "resolve_region": seams.resolve_region,
        "_resolve_dataset_selection": seams.resolve_selection,
        "_load_dataset": seams.load_dataset,
        "_build_simulation": seams.build_simulation,
        "SimulationOutputBuilder": seams.output_builder,
        "LocalAreaBreakdownBuilder": seams.breakdown_builder,
    }.items():
        monkeypatch.setattr(
            f"policyengine_simulation_executor.simulation_runtime.{name}", value
        )
    return run


def _run_routed_uk(monkeypatch, *, params, local_area_dataset) -> _UKAreaRun:
    run = _install_routed_uk_fakes(monkeypatch, local_area_dataset=local_area_dataset)
    run.output = _run_simulation_impl_core(dict(params), runtime=run.runtime)
    return run


def _span_attributes(nodes) -> list[dict]:
    """Return the attributes of every span in a tracked span tree."""
    attributes = []
    for node in nodes:
        attributes.append(node["attrs"])
        attributes.extend(_span_attributes(node["children"]))
    return attributes


@pytest.mark.parametrize("region", ["uk", "country/wales"])
def test_a_routed_uk_wide_or_nation_run_builds_no_area_pair(monkeypatch, region):
    from policyengine_simulation_executor import uk_local_authority_metadata

    run = _run_routed_uk(
        monkeypatch,
        params={**_WALES_NATIONAL_PARAMS, "region": region},
        local_area_dataset=_routed_area_dataset(),
    )

    # One dataset, the national file, and one baseline and reform on it.
    assert run.loads == [_NATIONAL_SELECTION]
    assert [build["selection"] for build in run.builds] == [_NATIONAL_SELECTION] * 2
    (builder_kwargs,) = run.builder_calls
    assert builder_kwargs["baseline"] is run.builds[0]["simulation"]
    assert builder_kwargs["reform"] is run.builds[1]["simulation"]
    assert builder_kwargs["omit_uk_area_breakdowns"] is True
    assert builder_kwargs["omit_uk_area_breakdowns"] is (
        uk_local_authority_metadata.uk_area_breakdowns_omitted("uk", region)
    )
    assert "uk_area_simulations" not in builder_kwargs
    assert run.serialized == ["macro"]
    assert {"dataset_role": "uk_area"} not in _span_attributes(run.runtime.roots)


@pytest.mark.parametrize(
    "region", ["constituency/E14001063", "local_authority/E06000063"]
)
@pytest.mark.parametrize(
    "missing",
    ["constituency_code_oa", "la_code_oa", "region_code_oa", "source_household_id"],
)
def test_a_routed_area_run_needs_every_column_its_outputs_read_before_simulating(
    monkeypatch, region, missing
):
    run = _install_routed_uk_fakes(
        monkeypatch, local_area_dataset=_routed_area_dataset(without=missing)
    )

    with pytest.raises(
        ValueError,
        match=rf"'microcosm_uk_2024_25_dense', which the bundle routes .* has no "
        rf"{missing} column",
    ):
        _run_simulation_impl_core(
            {**_WALES_NATIONAL_PARAMS, "region": region}, runtime=run.runtime
        )

    assert run.loads == [_LOCAL_AREA_SELECTION]
    assert run.builds == []


def test_a_routed_area_run_with_every_column_detects_its_boundaries(monkeypatch):
    run = _run_routed_uk(
        monkeypatch,
        params={**_WALES_NATIONAL_PARAMS, "region": "local_authority/E06000063"},
        local_area_dataset=_routed_area_dataset(),
    )

    (builder_kwargs,) = run.builder_calls
    assert builder_kwargs["uk_local_authority_metadata"] == UKLocalAuthorityMetadata(
        boundary_version=UKLocalAuthorityBoundaryVersion.LAD23
    )
    assert builder_kwargs["omit_uk_area_breakdowns"] is False


def test_a_local_area_breakdown_runs_on_the_local_area_dataset_alone(monkeypatch):
    local_area_dataset = _routed_area_dataset()
    monkeypatch.setattr(
        "policyengine_simulation_executor.simulation_runtime."
        "_manifest_dataset_revision",
        lambda country, name: {
            ("uk", _LOCAL_AREA_SELECTION.name): _LOCAL_AREA_REVISION
        }[(country, name)],
    )

    run = _run_routed_uk(
        monkeypatch,
        params=_WALES_BREAKDOWN_PARAMS,
        local_area_dataset=local_area_dataset,
    )

    # The national file is never loaded: one load, one baseline and reform.
    assert run.loads == [_LOCAL_AREA_SELECTION]
    assert [build["selection"] for build in run.builds] == [_LOCAL_AREA_SELECTION] * 2
    assert all(build["dataset"] is local_area_dataset for build in run.builds)
    assert all(build["scoping_strategy"] is _WALES for build in run.builds)
    assert [build["policy"] for build in run.builds] == [
        normalise_policy(_TEST_POLICIES["baseline"]),
        normalise_policy(_TEST_POLICIES["reform"]),
    ]
    (builder_kwargs,) = run.builder_calls
    assert builder_kwargs["baseline"] is run.builds[0]["simulation"]
    assert builder_kwargs["reform"] is run.builds[1]["simulation"]
    assert builder_kwargs["local_authority_metadata"] == UKLocalAuthorityMetadata(
        boundary_version=UKLocalAuthorityBoundaryVersion.LAD23
    )
    assert builder_kwargs["dataset_name"] == _LOCAL_AREA_SELECTION.name
    assert builder_kwargs["data_version"] == _LOCAL_AREA_REVISION
    assert builder_kwargs["year"] == 2026
    assert run.serialized == ["local_area_breakdown"]
    assert run.output == {"kind": "local_areas"}
    assert run.dataset_load_attributes() == [{"dataset_role": "uk_area"}]


def test_a_scotland_breakdown_resolves_against_the_installed_bundle(monkeypatch):
    # Only the dataset load, the simulations and the builder are faked: the
    # country module, region resolution, dataset selection, routing and the
    # revision come from the installed .py bundle, with no data or network.
    from policyengine.provenance.manifest import get_release_manifest
    from policyengine_simulation_executor import simulation_runtime

    loads: list[DatasetSelection] = []
    builds: list[dict] = []
    builder_calls: list[dict] = []

    def load_dataset(_params, *, selection, country_module, data_folder=None):
        loads.append(selection)
        return _routed_area_dataset()

    def build_simulation(_params, *, dataset, dataset_selection, policy, region):
        builds.append(
            {
                "selection": dataset_selection,
                "region_code": region.code,
                "scoping_strategy": region.scoping_strategy,
            }
        )
        return SimpleNamespace(on_first_ensure=lambda _callback: None)

    class RecordingBreakdownBuilder:
        def __init__(self, **kwargs):
            builder_calls.append(kwargs)

        def serialize(self):
            return {"kind": "local_areas"}

    for name, value in {
        "_load_dataset": load_dataset,
        "_build_simulation": build_simulation,
        "LocalAreaBreakdownBuilder": RecordingBreakdownBuilder,
    }.items():
        monkeypatch.setattr(simulation_runtime, name, value)

    output = _run_simulation_impl_core(
        {**_WALES_BREAKDOWN_PARAMS, "region": "country/scotland"},
        runtime=TrackingRuntime(),
    )

    (selection,) = loads
    assert selection.name == "microcosm_uk_2024_25_dense"
    assert selection.is_default is False
    scotland = RowFilterStrategy(variable_name="region", variable_value="SCOTLAND")
    assert [build["scoping_strategy"] for build in builds] == [scotland, scotland]
    assert [build["selection"] for build in builds] == [selection, selection]
    assert {build["region_code"] for build in builds} == {"country/scotland"}
    (builder_kwargs,) = builder_calls
    assert builder_kwargs["dataset_name"] == selection.name
    assert builder_kwargs["data_version"] == (
        get_release_manifest("uk").datasets[selection.name].revision
    )
    assert output == {"kind": "local_areas"}


def test_a_dataset_revision_is_read_from_the_release_manifest(monkeypatch):
    from policyengine_simulation_executor import simulation_runtime

    manifests = {
        "uk": SimpleNamespace(
            datasets={
                _LOCAL_AREA_SELECTION.name: SimpleNamespace(
                    revision=_LOCAL_AREA_REVISION
                ),
                "unrevised": SimpleNamespace(revision=""),
            }
        )
    }
    monkeypatch.setattr(
        "policyengine.provenance.manifest.get_release_manifest", manifests.__getitem__
    )

    revision = simulation_runtime._manifest_dataset_revision
    assert revision("uk", _LOCAL_AREA_SELECTION.name) == _LOCAL_AREA_REVISION
    assert revision("uk", "unrevised") is None
    assert revision("uk", "absent") is None


def test_a_uk_wide_local_area_breakdown_simulates_the_whole_dataset(monkeypatch):
    run = _run_routed_uk(
        monkeypatch,
        params={**_WALES_BREAKDOWN_PARAMS, "region": "uk"},
        local_area_dataset=_routed_area_dataset(),
    )

    assert run.loads == [_LOCAL_AREA_SELECTION]
    assert [build["scoping_strategy"] for build in run.builds] == [None, None]
    assert run.serialized == ["local_area_breakdown"]


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"country": "us"}, "only for UK requests"),
        ({"region": "constituency/E14001063"}, "'constituency/E14001063'"),
        ({"region": "local_authority/E06000063"}, "'local_authority/E06000063'"),
        ({"include_cliffs": True}, "include_cliffs=true"),
        ({"region": None, "region_group": ["country/wales"]}, "region_group"),
        ({"breakdown": "districts"}, "'districts'"),
    ],
)
def test_an_invalid_local_area_breakdown_is_refused_before_any_load(
    monkeypatch, change, message
):
    run = _install_routed_uk_fakes(
        monkeypatch, local_area_dataset=_routed_area_dataset()
    )

    with pytest.raises(ValueError, match=message):
        _run_simulation_impl_core(
            {**_WALES_BREAKDOWN_PARAMS, **change}, runtime=run.runtime
        )

    assert run.country_modules == []
    assert run.loads == []
    assert run.builds == []


def test_a_local_area_breakdown_needs_a_bundle_that_routes_area_regions(monkeypatch):
    run = _install_routed_uk_fakes(
        monkeypatch, local_area_dataset=_routed_area_dataset(), routed=False
    )

    with pytest.raises(
        ValueError,
        match="does not route UK constituency and local-authority regions",
    ):
        _run_simulation_impl_core(dict(_WALES_BREAKDOWN_PARAMS), runtime=run.runtime)

    assert run.loads == []
    assert run.builds == []


@pytest.mark.parametrize(
    "missing",
    ["constituency_code_oa", "la_code_oa", "region_code_oa", "source_household_id"],
)
def test_a_local_area_breakdown_needs_every_column_its_outputs_read(
    monkeypatch, missing
):
    local_area_dataset = _routed_area_dataset(without=missing)

    with pytest.raises(
        ValueError,
        match=rf"'microcosm_uk_2024_25_dense', which the bundle routes .* has no "
        rf"{missing} column",
    ):
        _run_routed_uk(
            monkeypatch,
            params=_WALES_BREAKDOWN_PARAMS,
            local_area_dataset=local_area_dataset,
        )


_LOCAL_AREA_URI = (
    "hf://policyengine/populace-uk-private/microcosm_uk_2024_25_dense.h5"
    "@microcosm-uk-2024-25-dense-20261019T000432Z-ee5c2dbe"
)


def _registry_area(region_type: str, code: str, label: str) -> SimpleNamespace:
    return SimpleNamespace(
        code=f"{region_type}/{code}",
        label=label,
        region_type=region_type,
        parent_code="uk",
        dataset_path=_LOCAL_AREA_URI,
        scoping_strategy=object(),
    )


def _routed_uk_country_module() -> SimpleNamespace:
    """Return a UK model whose registry lists area regions, as routing gives."""
    areas = [
        _registry_area("constituency", "E14001063", "Aldershot"),
        _registry_area("constituency", "W07000112", "Ynys Môn"),
        _registry_area("local_authority", "E07000110", "Maidstone"),
        _registry_area("local_authority", "N09000011", "Ards and North Down"),
    ]
    by_code = {area.code: area for area in areas}
    registry = SimpleNamespace(
        get_by_type=lambda region_type: [
            area for area in areas if area.region_type == region_type
        ]
    )
    return SimpleNamespace(
        model=SimpleNamespace(get_region=by_code.get, region_registry=registry)
    )


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        ("constituency/E14001063", "constituency/E14001063"),
        ("constituency/e14001063", "constituency/E14001063"),
        ("constituency/Aldershot", "constituency/E14001063"),
        ("constituency/ynys môn", "constituency/W07000112"),
        ("local_authority/Maidstone", "local_authority/E07000110"),
        ("local_authority/n09000011", "local_authority/N09000011"),
        ("local_authority/Ards and North Down", "local_authority/N09000011"),
    ],
)
def test_resolve_region_maps_routed_uk_area_names_and_codes_to_the_registry(
    monkeypatch, requested, expected
):
    _routes_uk_area_regions(monkeypatch, routed=True)
    country_module = _routed_uk_country_module()

    resolution = resolve_region(
        country_module=country_module, country="uk", params={"region": requested}
    )

    region = country_module.model.get_region(expected)
    assert resolution.code == expected
    assert resolution.scoping_strategy is region.scoping_strategy
    assert resolution.dataset_reference == _LOCAL_AREA_URI


@pytest.mark.parametrize(
    "requested",
    [
        # A district that April 2023 replaced: LAD22 only.
        "local_authority/E07000026",
        "local_authority/Carlisle",
        "constituency/Maidstone",
    ],
)
def test_resolve_region_rejects_uk_areas_outside_the_routed_registry(
    monkeypatch, requested
):
    _routes_uk_area_regions(monkeypatch, routed=True)

    with pytest.raises(ValueError, match="Unsupported UK region"):
        resolve_region(
            country_module=_routed_uk_country_module(),
            country="uk",
            params={"region": requested},
        )


@pytest.mark.parametrize(
    "requested", ["constituency/Aldershot", "local_authority/E07000110"]
)
def test_resolve_region_rejects_uk_area_regions_on_an_unrouted_bundle(
    monkeypatch, requested
):
    # Without routing, policyengine.py's registry lists no area regions, and
    # no weight-matrix region stands in for one: older bundles keep their own
    # frozen apps.
    _routes_uk_area_regions(monkeypatch, routed=False)
    country_module = SimpleNamespace(model=SimpleNamespace(get_region=lambda _: None))

    with pytest.raises(ValueError, match=f"Unsupported UK region: {requested}"):
        resolve_region(
            country_module=country_module, country="uk", params={"region": requested}
        )


class _BreakdownSimulation:
    """A local-area simulation that records when it is computed."""

    def __init__(self, label, events):
        self.label = label
        self.events = events

    def ensure(self):
        self.events.append(self.label)


class _RecordingProgress:
    def __init__(self, events):
        self.events = events

    def enter(self, stage):
        self.events.append(stage)


def _breakdown_builder(monkeypatch, *, events, runtime=None):
    """Return a UK breakdown builder whose area outputs are faked.

    The country module's impact analysis fails the test if it is called.
    Each fake records into ``events``.
    """
    disclosure = simulation_output_geographic.UKGeographyDisclosureOutput()

    def compute(country, baseline_simulation, reform_simulation, *, year):
        events.append(
            ("release", country, baseline_simulation, reform_simulation, year)
        )
        return disclosure

    def constituencies(country, value):
        events.append(("constituency", value))
        return GeographicImpactOutput.model_validate(
            [constituency_impact_record().model_dump(mode="json")]
        )

    def local_authorities(country, value, **kwargs):
        events.append(("local_authority", value, kwargs["uk_local_authority_metadata"]))
        return GeographicImpactOutput.model_validate([])

    for name, function in {
        "compute_uk_geography_disclosure_output": compute,
        "build_uk_constituency_impact": constituencies,
        "build_uk_local_authority_impact": local_authorities,
    }.items():
        monkeypatch.setattr(
            f"policyengine_simulation_executor.simulation_output_geographic.{name}",
            function,
        )

    def no_impact_analysis(*_args, **_kwargs):
        pytest.fail("a local-area breakdown computes no national impact analysis")

    builder = LocalAreaBreakdownBuilder(
        baseline=_BreakdownSimulation("baseline", events),
        reform=_BreakdownSimulation("reform", events),
        local_authority_metadata=UKLocalAuthorityMetadata(
            boundary_version=UKLocalAuthorityBoundaryVersion.LAD23
        ),
        dataset_name="microcosm_uk_2024_25_dense",
        data_version=_LOCAL_AREA_REVISION,
        year=2026,
        country_module=SimpleNamespace(
            model=SimpleNamespace(version="2.132.6"),
            economic_impact_analysis=no_impact_analysis,
        ),
        runtime=runtime,
        progress=_RecordingProgress(events),
    )
    return builder, disclosure


def test_a_local_area_breakdown_publishes_both_levels_from_one_release(monkeypatch):
    events: list[object] = []
    builder, disclosure = _breakdown_builder(monkeypatch, events=events)

    output = builder.serialize()

    releases = [event for event in events if event[0] == "release"]
    assert releases == [("release", "uk", builder.baseline, builder.reform, 2026)]
    assert [event for event in events if event[0] != "release"][-2:] == [
        ("constituency", disclosure),
        ("local_authority", disclosure, builder.local_authority_metadata),
    ]
    assert output == {
        "kind": "local_areas",
        "model_version": "2.132.6",
        "policyengine_version": get_country_release_bundle("uk").policyengine_version,
        "dataset": "microcosm_uk_2024_25_dense",
        "data_version": _LOCAL_AREA_REVISION,
        "constituency_impact": [constituency_impact_record().model_dump(mode="json")],
        "local_authority_impact": [],
    }


def test_a_local_area_breakdown_enters_outputs_after_both_simulations(monkeypatch):
    events: list[object] = []
    builder, _ = _breakdown_builder(monkeypatch, events=events)

    builder.serialize()

    simulations_and_stages = [event for event in events if not isinstance(event, tuple)]
    assert simulations_and_stages == ["baseline", "reform", JobStage.OUTPUTS]
    assert events.index(JobStage.OUTPUTS) < next(
        index for index, event in enumerate(events) if isinstance(event, tuple)
    )


def test_a_local_area_breakdown_opens_only_registered_spans(monkeypatch):
    events: list[object] = []
    runtime = TrackingRuntime()
    builder, _ = _breakdown_builder(monkeypatch, events=events, runtime=runtime)

    builder.serialize()

    registered = {stage.value for stage in ANNUAL_IMPACT_STAGES.stages}
    assert set(runtime.names) <= registered
    assert ANNUAL_IMPACT_STAGES.name(Stage.ECONOMIC_IMPACT_ANALYSIS) not in (
        runtime.names
    )
    (calculation,) = runtime.roots[:1]
    assert calculation["name"] == ANNUAL_IMPACT_STAGES.name(Stage.CALCULATION)
    (output_build,) = calculation["children"]
    assert output_build["name"] == ANNUAL_IMPACT_STAGES.name(
        Stage.SIMULATION_OUTPUT_BUILD
    )
    assert {
        ANNUAL_IMPACT_STAGES.name(Stage.OUTPUT_UK_CONSTITUENCY),
        ANNUAL_IMPACT_STAGES.name(Stage.OUTPUT_UK_LOCAL_AUTHORITY),
    } <= {child["name"] for child in output_build["children"]}
