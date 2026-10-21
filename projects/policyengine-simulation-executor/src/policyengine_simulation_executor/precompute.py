"""Library half of the artifact precompute (the write path of the pipeline).

``src/modal/precompute_app.py`` holds only the Modal plumbing — app, image,
function declarations, local entrypoint — and every function body lives
here, mirroring the deployed app's split (``run_simulation`` ->
``simulation_runtime.run_simulation_impl``). Keeping the logic in the
executor package makes it testable without Modal fakes and importable by
the CI deploy job and the GC planner.

Data flow is strictly typed via ``precompute_models``: plans, entries,
version identities, manifests, and worker results are Pydantic models
everywhere inside the library; plain dicts appear only at the Modal
serialization boundary (the app wrappers validate structured inputs on
the way in and dump model results on the way out; bare strings — the
bucket, the manifest digest — cross as-is).

The store is content-addressed, so a precompute run is idempotent: it
plans against the store and computes only misses; a re-run against a warm
store is a no-op. ``force`` recomputes everything but CANNOT replace
existing store objects (uploads are write-once) — healing a bad artifact
means deleting its object first, then re-running.

Cohort baselines are built through the executor's own request path
(``_resolve_region`` + ``_build_simulation`` on a synthetic child request),
so scoping, extra variables, and the deterministic simulation id match the
production child's by construction, not by convention — and a regression
test asserts the writer and reader derive identical identities. All
identity collection and store I/O must run inside containers: the bundle
receipt that the keys digest lives in the image, not on the deploying
machine.
"""

from __future__ import annotations

import gc
import logging
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from policyengine_simulation_executor.artifact_keys import (
    BaselineArtifactIdentity,
    baseline_artifact_filename,
)
from policyengine_simulation_executor.precompute_models import (
    ArtifactManifest,
    BaselineComputeResult,
    BaselinePlanEntry,
    BundleVersionIdentity,
    DatasetBuildResult,
    DatasetPlanEntry,
    DeterminismVerdict,
    ManifestArtifact,
    ManifestEntry,
    PrecomputePlan,
    RemoteFunction,
    StateDatasetArtifact,
    StateDatasetBuildResult,
    StateDatasetPlanEntry,
    WorkSelection,
)
from policyengine_simulation_executor.state_datasets import (
    state_dataset_relative_folder,
    year_file_reuse_problem,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import ModuleType

    from microdf import MicroDataFrame
    from policyengine.core import Simulation
    from policyengine.core.scoping_strategy import RowFilterStrategy

    from policyengine_simulation_executor.artifact_store import ArtifactStore
    from policyengine_simulation_executor.simulation_runtime import RegionResolution

logger = logging.getLogger(__name__)

PRECOMPUTE_COUNTRY = "us"
# The user-facing priority order (2026 first); with parallel waves it only
# orders spawn submission, but keep it intentional.
PRECOMPUTE_YEARS = [2026, 2027, 2025]

MANIFEST_SCHEMA = "mf1"

# The stdout contract the CI deploy job parses; keep format changes in
# lockstep with the workflow side (covered by a regression test).
MANIFEST_DIGEST_PREFIX = "MANIFEST_DIGEST="


def manifest_digest_line(digest: str) -> str:
    """Format the ``MANIFEST_DIGEST=<digest>`` line the CI deploy job parses."""
    return f"{MANIFEST_DIGEST_PREFIX}{digest}"


def cohort_params(year: int, group: list[str]) -> dict:
    """Return the synthetic request a segmented-national child would receive."""
    return {
        "country": PRECOMPUTE_COUNTRY,
        "scope": "macro",
        "region_group": list(group),
        "time_period": year,
    }


def _cohort_resolution(
    year: int, group: list[str]
) -> tuple[dict[str, Any], ModuleType, RegionResolution]:
    """Cohort request params, country module, and region resolution.

    The planner's identity (``cohort_identity``) and the worker's
    simulation (``_prepare_cohort_baseline``) must derive from ONE
    resolution or the in-container id checks abort the run; sharing this
    prologue removes the drift surface.
    """
    from policyengine_simulation_executor.simulation_runtime import (  # noqa: PLC0415  # resolved per call so tests can patch simulation_runtime helpers
        _country_module,
        _resolve_region,
    )

    params = cohort_params(year, group)
    country_module = _country_module(PRECOMPUTE_COUNTRY)
    resolution = _resolve_region(
        country_module=country_module,
        country=PRECOMPUTE_COUNTRY,
        params=params,
    )
    return params, country_module, resolution


def cohort_identity(year: int, group: list[str]) -> BaselineArtifactIdentity:
    """Identity via the executor's own resolution path (writer==reader)."""
    from policyengine_simulation_executor.baseline_artifacts import (
        BaselineIdentityRequest,
        qualifying_baseline_identity,
    )
    from policyengine_simulation_executor.simulation_runtime import (  # noqa: PLC0415  # resolved per call so tests can patch simulation_runtime helpers
        _resolve_dataset_selection,
    )

    params, _, resolution = _cohort_resolution(year, group)
    dataset_selection = _resolve_dataset_selection(params, region_resolution=resolution)
    identity = qualifying_baseline_identity(
        BaselineIdentityRequest(
            params,
            country=PRECOMPUTE_COUNTRY,
            dataset_is_default=dataset_selection.is_default,
            policy=None,
            region_code=resolution.code,
            scoping_strategy=resolution.scoping_strategy,
            year=year,
        )
    )
    if identity is None:
        msg = (
            f"Cohort request for year {year} group {group} does not qualify "
            "for a deterministic baseline id — writer and reader predicates "
            "have diverged."
        )
        raise RuntimeError(msg)
    return identity


@dataclass(frozen=True)
class StateDatasetTarget:
    """A US state whose requests load a non-default dataset, and its row filter.

    The precompute bakes this state's rows of ``dataset`` as their own year
    file: the households whose ``filter_variable`` equals ``state_fips``.
    ``scoping_strategy`` is the region's own .py strategy, the one a request
    for the state applies, so a baked file holds exactly the rows the request
    would keep.
    """

    region: str
    state_code: str
    state_fips: int
    filter_variable: str
    dataset: str
    scoping_strategy: RowFilterStrategy | None = field(
        default=None, compare=False, repr=False
    )


def state_dataset_targets() -> list[StateDatasetTarget]:
    """Return every US state region that loads a non-default dataset, by state code.

    Each state is resolved exactly as a request for it is (``_resolve_region``
    then ``_resolve_dataset_selection``), so a bundle that sends states back to
    the default dataset gets no state files.
    """
    from policyengine_simulation_executor.simulation_runtime import (  # noqa: PLC0415  # resolved per call so tests can patch simulation_runtime helpers
        _country_module,
        _resolve_dataset_selection,
        _resolve_region,
    )

    country_module = _country_module(PRECOMPUTE_COUNTRY)
    targets = []
    for region in country_module.model.region_registry.get_by_type("state"):
        params = {
            "country": PRECOMPUTE_COUNTRY,
            "region": region.code,
            "time_period": PRECOMPUTE_YEARS[0],
        }
        resolution = _resolve_region(
            country_module=country_module, country=PRECOMPUTE_COUNTRY, params=params
        )
        selection = _resolve_dataset_selection(params, region_resolution=resolution)
        if selection.is_default:
            continue
        strategy = resolution.scoping_strategy
        variable_name = getattr(strategy, "variable_name", None)
        variable_value = getattr(strategy, "variable_value", None)
        if not variable_name or variable_value is None or not resolution.state_code:
            msg = (
                f"State region {resolution.code} loads {selection.name} but has no "
                "row filter and state code to build its state file from"
            )
            raise RuntimeError(msg)
        targets.append(
            StateDatasetTarget(
                region=resolution.code,
                state_code=resolution.state_code,
                state_fips=int(variable_value),
                filter_variable=variable_name,
                dataset=selection.name,
                scoping_strategy=strategy,
            )
        )
    return sorted(targets, key=lambda target: target.state_code)


def select_work(plan: PrecomputePlan, *, force: bool) -> WorkSelection:
    """Which plan entries need computing."""
    return WorkSelection(
        datasets=[entry for entry in plan.datasets if force or not entry.exists],
        baselines=[entry for entry in plan.baselines if force or not entry.exists],
        state_datasets=[
            entry for entry in plan.state_datasets if force or not entry.exists
        ],
    )


def build_manifest(plan: PrecomputePlan) -> ArtifactManifest:
    """Build the deploy manifest: the receipt and each artifact the image must fetch.

    Datasets, then baselines, then per-state datasets, each in plan order, so
    a plan without per-state datasets keeps the manifest it always had.
    """
    artifacts: list[ManifestEntry] = [
        ManifestArtifact(
            type="dataset",
            path=entry.path,
            filename=entry.filename,
            year=entry.year,
            digest=entry.digest,
        )
        for entry in plan.datasets
    ] + [
        ManifestArtifact(
            type="baseline",
            path=entry.path,
            filename=entry.path.rsplit("/", maxsplit=1)[-1],
            year=entry.year,
            digest=entry.digest,
        )
        for entry in plan.baselines
    ]
    artifacts += [
        StateDatasetArtifact(
            type="state_dataset",
            path=entry.path,
            filename=entry.filename,
            folder=entry.folder,
            state_code=entry.state_code,
            year=entry.year,
            digest=entry.digest,
        )
        for entry in plan.state_datasets
    ]
    return ArtifactManifest(
        manifest_schema=MANIFEST_SCHEMA,
        country=PRECOMPUTE_COUNTRY,
        receipt=plan.receipt,
        artifacts=artifacts,
    )


def plan_artifacts_impl(bucket: str) -> PrecomputePlan:
    """Compute every expected artifact identity and its store presence."""
    from policyengine_simulation_executor.artifact_keys import (  # noqa: PLC0415  # resolved per call so tests can patch artifact_keys.collect_dataset_identity
        collect_dataset_identity,
        collect_state_dataset_identity,
    )
    from policyengine_simulation_executor.artifact_store import (  # noqa: PLC0415  # resolved per call so tests can patch artifact_store.ArtifactStore
        ArtifactStore,
    )
    from policyengine_simulation_executor.national_partition import (  # noqa: PLC0415  # resolved per call so tests can patch national_partition.national_region_groups
        national_region_groups,
    )
    from policyengine_simulation_executor.release_bundle import (  # noqa: PLC0415  # resolved per call so tests can patch release_bundle.get_country_release_bundle
        get_country_release_bundle,
    )

    store = ArtifactStore(bucket)
    groups = national_region_groups(PRECOMPUTE_COUNTRY)
    if not groups:
        msg = f"No national partition for {PRECOMPUTE_COUNTRY!r}"
        raise RuntimeError(msg)

    targets = state_dataset_targets()
    datasets: list[DatasetPlanEntry] = []
    baselines: list[BaselinePlanEntry] = []
    state_datasets: list[StateDatasetPlanEntry] = []
    for year in PRECOMPUTE_YEARS:
        dataset_identity = collect_dataset_identity(PRECOMPUTE_COUNTRY, year)
        datasets.append(
            DatasetPlanEntry(
                year=year,
                digest=dataset_identity.digest,
                path=dataset_identity.store_path,
                filename=dataset_identity.filename,
                exists=store.exists(dataset_identity.store_path),
            )
        )
        for group in groups:
            identity = cohort_identity(year, group)
            baselines.append(
                BaselinePlanEntry(
                    year=year,
                    group=list(group),
                    region=identity.region,
                    digest=identity.digest,
                    path=identity.store_path,
                    simulation_id=identity.simulation_id,
                    exists=store.exists(identity.store_path),
                )
            )
        for target in targets:
            state_identity = collect_state_dataset_identity(
                PRECOMPUTE_COUNTRY,
                year,
                dataset=target.dataset,
                state_code=target.state_code,
                state_fips=target.state_fips,
                filter_variable=target.filter_variable,
            )
            state_datasets.append(
                StateDatasetPlanEntry(
                    year=year,
                    state_code=target.state_code,
                    region=target.region,
                    dataset=target.dataset,
                    digest=state_identity.digest,
                    path=state_identity.store_path,
                    filename=state_identity.filename,
                    folder=state_dataset_relative_folder(
                        target.dataset, target.state_code
                    ),
                    exists=store.exists(state_identity.store_path),
                )
            )

    bundle = get_country_release_bundle(PRECOMPUTE_COUNTRY)
    receipt = BundleVersionIdentity(
        policyengine_version=bundle.policyengine_version,
        model_version=bundle.model_version,
        data_version=bundle.data_version,
        data_artifact_revision=bundle.data_artifact_revision,
        default_dataset=bundle.default_dataset,
    )
    return PrecomputePlan(
        datasets=datasets,
        baselines=baselines,
        receipt=receipt,
        state_datasets=state_datasets,
    )


def build_dataset_impl(bucket: str, expected: DatasetPlanEntry) -> DatasetBuildResult:
    """Wave 1: build one single-year dataset and upload it."""
    from policyengine_simulation_executor.artifact_keys import (  # noqa: PLC0415  # resolved per call so tests can patch artifact_keys.collect_dataset_identity
        collect_dataset_identity,
    )
    from policyengine_simulation_executor.artifact_store import (  # noqa: PLC0415  # resolved per call so tests can patch artifact_store.ArtifactStore
        ArtifactStore,
    )
    from policyengine_simulation_executor.release_bundle import (  # noqa: PLC0415  # resolved per call so tests can patch release_bundle.get_country_release_bundle
        get_country_release_bundle,
    )
    from policyengine_simulation_executor.simulation_runtime import (  # noqa: PLC0415  # resolved per call so tests can patch simulation_runtime helpers
        _country_module,
        resolve_data_folder,
    )

    identity = collect_dataset_identity(PRECOMPUTE_COUNTRY, expected.year)
    if identity.store_path != expected.path:
        msg = (
            "Planned and in-container dataset identities disagree "
            f"({expected.path} != {identity.store_path}); refusing to upload "
            "under a mismatched key."
        )
        raise RuntimeError(msg)

    data_folder = resolve_data_folder()
    country_module = _country_module(PRECOMPUTE_COUNTRY)
    started = time.monotonic()
    country_module.ensure_datasets(
        datasets=[get_country_release_bundle(PRECOMPUTE_COUNTRY).default_dataset],
        years=[expected.year],
        data_folder=data_folder,
    )
    build_seconds = time.monotonic() - started

    local_file = Path(data_folder) / identity.filename
    if not local_file.exists():
        msg = f"ensure_datasets produced no file at {local_file}"
        raise RuntimeError(msg)
    uploaded = ArtifactStore(bucket).upload_file(identity.store_path, local_file)
    return DatasetBuildResult(
        year=expected.year,
        path=identity.store_path,
        uploaded=uploaded,
        build_seconds=round(build_seconds, 1),
        size_bytes=local_file.stat().st_size,
    )


def _checked_state_targets(
    year: int, expected: list[StateDatasetPlanEntry]
) -> dict[str, StateDatasetTarget]:
    """Return each planned state's target, after re-deriving its key in this container.

    The planner and this builder run in different containers; both derive a
    state's key from the installed bundle. A disagreement means they see
    different bundles, so nothing is built rather than uploading a file under
    a key that does not describe it.
    """
    from policyengine_simulation_executor.artifact_keys import (  # noqa: PLC0415  # resolved per call so tests can patch artifact_keys.collect_state_dataset_identity
        collect_state_dataset_identity,
    )

    targets = {target.region: target for target in state_dataset_targets()}
    datasets = set()
    for entry in expected:
        if entry.year != year:
            msg = (
                f"State dataset entry for {entry.state_code} is for year "
                f"{entry.year}, not {year}"
            )
            raise ValueError(msg)
        target = targets.get(entry.region)
        if target is None:
            msg = f"No state dataset target for {entry.region} in this container"
            raise RuntimeError(msg)
        identity = collect_state_dataset_identity(
            PRECOMPUTE_COUNTRY,
            year,
            dataset=target.dataset,
            state_code=target.state_code,
            state_fips=target.state_fips,
            filter_variable=target.filter_variable,
        )
        folder = state_dataset_relative_folder(target.dataset, target.state_code)
        if (identity.store_path, identity.filename, folder) != (
            entry.path,
            entry.filename,
            entry.folder,
        ):
            msg = (
                f"Planned and in-container identities of {entry.region} {year} "
                f"disagree ({entry.path} != {identity.store_path}); refusing to "
                "build under a mismatched key."
            )
            raise RuntimeError(msg)
        datasets.add(target.dataset)
    if len(datasets) > 1:
        msg = f"One state dataset build covers one dataset, not {sorted(datasets)}"
        raise ValueError(msg)
    return {entry.region: targets[entry.region] for entry in expected}


def _registered_district_geoids(country_module: ModuleType) -> set[int]:
    """Return the district codes the US congressional-district regions filter on."""
    return {
        int(region.scoping_strategy.variable_value)
        for region in country_module.model.region_registry.get_by_type(
            "congressional_district"
        )
    }


def _require_districts_within_their_states(
    household: MicroDataFrame, district_geoids: set[int]
) -> None:
    """Refuse a dataset in which a household sits in another state's district.

    District requests read their state's file and filter it by
    ``congressional_district_geoid`` (state FIPS times 100 plus the district
    number). A household whose code names a district of another state would
    be missing from that district's results, so the build stops instead. A
    code that names no district, such as an unassigned 0, matches no district
    request on either path and changes no result. Both columns are labels;
    nothing here is weighted.
    """
    if "congressional_district_geoid" not in household.columns:
        return
    district = household["congressional_district_geoid"]
    in_another_state = district.isin(district_geoids) & (
        (district // 100) != household["state_fips"]
    )
    if in_another_state.any():
        msg = (
            "Some households have a congressional district code that names "
            "another state's district; their state's file would not match "
            "that district's results"
        )
        raise RuntimeError(msg)


def build_state_datasets_impl(
    bucket: str,
    year: int,
    expected: list[StateDatasetPlanEntry],
    *,
    work_folder: Path | None = None,
) -> list[StateDatasetBuildResult]:
    """Build, check and upload one year's per-state datasets.

    Prepares the regional dataset's full-country year file once, then for
    each planned state keeps the rows its region's own scoping strategy keeps,
    writes them with .py's ``save()``, copies the year-file stamp, confirms
    that .py's reuse checks accept the file, and uploads it.
    """
    from policyengine.tax_benefit_models.us.datasets import (
        PolicyEngineUSDataset,
        USYearData,
    )
    from policyengine.tax_benefit_models.us.legacy_inputs import (
        read_year_file_stamp,
        write_year_file_stamp,
    )
    from policyengine.tax_benefit_models.us.model import US_GROUP_ENTITIES

    from policyengine_simulation_executor.artifact_store import (  # noqa: PLC0415  # resolved per call so tests can patch artifact_store.ArtifactStore
        ArtifactStore,
    )
    from policyengine_simulation_executor.simulation_runtime import (  # noqa: PLC0415  # resolved per call so tests can patch simulation_runtime helpers
        _country_module,
    )

    if not expected:
        return []
    started = time.monotonic()
    targets = _checked_state_targets(year, expected)
    dataset = expected[0].dataset
    work_folder = work_folder or Path(
        tempfile.mkdtemp(prefix="policyengine-state-datasets-")
    )
    source_folder = str(work_folder / "source")
    country_module = _country_module(PRECOMPUTE_COUNTRY)

    # Preparing the year file runs a Microsimulation over every state, whose
    # objects hold reference cycles; release them before filtering 51 copies.
    country_module.ensure_datasets(
        datasets=[dataset], years=[year], data_folder=source_folder
    )
    gc.collect()
    (source,) = country_module.load_datasets(
        datasets=[dataset], years=[year], data_folder=source_folder
    ).values()
    stamp = read_year_file_stamp(source.filepath)
    if stamp is None:
        msg = f"The prepared year file {source.filepath} has no year-file stamp"
        raise RuntimeError(msg)
    _require_districts_within_their_states(
        source.data.household, _registered_district_geoids(country_module)
    )

    store = ArtifactStore(bucket)
    results = []
    for entry in expected:
        strategy = targets[entry.region].scoping_strategy
        if strategy is None:
            msg = f"{entry.region} has no row filter to build its state file with"
            raise RuntimeError(msg)
        rows = strategy.apply(
            entity_data=source.data.entity_data,
            group_entities=US_GROUP_ENTITIES,
            year=year,
        )
        local_file = work_folder / entry.folder / entry.filename
        PolicyEngineUSDataset(
            name=f"{dataset}-{entry.state_code.lower()}-year-{year}",
            description=f"{entry.region} rows of {dataset} for {year}",
            filepath=str(local_file),
            year=year,
            metadata=dict(source.metadata),
            data=USYearData(**rows),
        ).save()
        # save() rewrites the file without the stamp, so stamp it afterwards,
        # as create_datasets does.
        write_year_file_stamp(local_file, stamp)
        problem = year_file_reuse_problem(local_file, dataset)
        if problem is not None:
            msg = f"ensure_datasets would not reuse {local_file}: {problem}"
            raise RuntimeError(msg)
        results.append(
            StateDatasetBuildResult(
                year=year,
                state_code=entry.state_code,
                path=entry.path,
                uploaded=store.upload_file(entry.path, local_file),
                size_bytes=local_file.stat().st_size,
                build_seconds=round(time.monotonic() - started, 1),
            )
        )
    return results


def _prepare_cohort_baseline(
    bucket: str, expected: BaselinePlanEntry
) -> tuple[ArtifactStore, Path, Simulation]:
    """Shared by compute and verify: dataset download + simulation build."""
    from policyengine_simulation_executor.artifact_keys import (  # noqa: PLC0415  # resolved per call so tests can patch artifact_keys.collect_dataset_identity
        collect_dataset_identity,
    )
    from policyengine_simulation_executor.artifact_store import (  # noqa: PLC0415  # resolved per call so tests can patch artifact_store.ArtifactStore
        ArtifactStore,
    )
    from policyengine_simulation_executor.simulation_runtime import (  # noqa: PLC0415  # resolved per call so tests can patch simulation_runtime helpers
        _build_simulation,
        _load_dataset,
        _resolve_dataset_selection,
        resolve_data_folder,
    )

    store = ArtifactStore(bucket)
    data_folder = Path(resolve_data_folder())

    dataset_identity = collect_dataset_identity(PRECOMPUTE_COUNTRY, expected.year)
    local_dataset = data_folder / dataset_identity.filename
    if not local_dataset.exists():
        store.download_file(dataset_identity.store_path, local_dataset)

    params, country_module, resolution = _cohort_resolution(
        expected.year, expected.group
    )
    dataset_selection = _resolve_dataset_selection(params, region_resolution=resolution)
    dataset = _load_dataset(
        params, country_module=country_module, selection=dataset_selection
    )
    baseline = _build_simulation(
        params,
        dataset=dataset,
        dataset_selection=dataset_selection,
        policy=None,
        region=resolution,
    )
    if baseline.id != expected.simulation_id:
        msg = (
            "Planned and in-container baseline ids disagree "
            f"({expected.simulation_id} != {baseline.id}); refusing to act "
            "under a mismatched key."
        )
        raise RuntimeError(msg)
    planned_storage_id = Path(expected.path).stem
    storage_id = getattr(baseline, "storage_id", baseline.id)
    if storage_id != planned_storage_id:
        msg = (
            "Planned and in-container baseline storage ids disagree "
            f"({planned_storage_id} != {storage_id}); refusing to act "
            "under a mismatched key."
        )
        raise RuntimeError(msg)

    # The extras economic_impact_analysis applies unconditionally before
    # ensure(); the artifact must carry them or every request would fail
    # the column guard and recompute.
    from policyengine.tax_benefit_models.us.analysis import (
        configure_budgetary_impact_variables,
    )

    configure_budgetary_impact_variables(baseline, baseline)
    return store, data_folder, baseline


def compute_baseline_impl(
    bucket: str, expected: BaselinePlanEntry
) -> BaselineComputeResult:
    """Wave 2: compute one cohort baseline and upload its output."""
    from policyengine_simulation_executor.baseline_artifacts import (
        ArtifactBaselineSimulation,
    )

    store, data_folder, baseline = _prepare_cohort_baseline(bucket, expected)
    if not isinstance(baseline, ArtifactBaselineSimulation):
        msg = (
            "Precompute built a plain Simulation — the qualifying predicate "
            "rejected the cohort request shape."
        )
        raise RuntimeError(msg)  # noqa: TRY004  # a precompute invariant failure, not a bad argument; raised across the compute_baseline Modal boundary and asserted by test_precompute

    started = time.monotonic()
    baseline.ensure()
    compute_seconds = time.monotonic() - started

    artifact_file = data_folder / baseline_artifact_filename(
        getattr(baseline, "storage_id", baseline.id)
    )
    if not artifact_file.exists():
        msg = f"ensure() left no artifact at {artifact_file}"
        raise RuntimeError(msg)
    uploaded = store.upload_file(expected.path, artifact_file)
    return BaselineComputeResult(
        year=expected.year,
        group=list(expected.group),
        simulation_id=baseline.id,
        outcome=baseline.artifact_outcome,
        uploaded=uploaded,
        compute_seconds=round(compute_seconds, 1),
        size_bytes=artifact_file.stat().st_size,
    )


def verify_determinism_impl(
    bucket: str, expected: BaselinePlanEntry
) -> DeterminismVerdict:
    """Load the uploaded artifact, recompute independently, compare frames.

    Exact comparison of every entity column (values and dtypes) between the
    store artifact (downloaded and loaded through ``ensure()``, so the save/
    load round-trip is under test too) and a fresh independent run — the
    direct check of "same key, same bytes". File-level byte equality is
    deliberately not used (HDF5 container metadata is not guaranteed
    stable).
    """
    from policyengine.core import (  # noqa: PLC0415  # resolved per call so tests can patch policyengine.core.Simulation
        Simulation,
    )

    from policyengine_simulation_executor.baseline_artifacts import (
        OUTCOME_HIT,
    )

    store, data_folder, baseline = _prepare_cohort_baseline(bucket, expected)
    artifact_file = data_folder / baseline_artifact_filename(
        getattr(baseline, "storage_id", baseline.id)
    )
    if not artifact_file.exists():
        store.download_file(expected.path, artifact_file)
    baseline.ensure()
    if baseline.artifact_outcome != OUTCOME_HIT:
        msg = (
            "Determinism gate could not load the artifact it verifies "
            f"(outcome={baseline.artifact_outcome})"
        )
        raise RuntimeError(msg)

    selection = getattr(baseline, "spm_config", None)
    fresh = Simulation(
        **({"spm": selection} if selection is not None else {}),
        dataset=baseline.dataset,
        tax_benefit_model_version=baseline.tax_benefit_model_version,
        policy=None,
        scoping_strategy=baseline.scoping_strategy,
        extra_variables=dict(baseline.extra_variables),
    )
    fresh.run()

    differences = []
    loaded = baseline.output_dataset.data.entity_data
    recomputed = fresh.output_dataset.data.entity_data
    for entity in sorted(set(loaded) | set(recomputed)):
        left, right = loaded.get(entity), recomputed.get(entity)
        if left is None or right is None:
            differences.append(f"{entity}: missing on one side")
            continue
        left_cols, right_cols = set(left.columns), set(right.columns)
        differences.extend(
            f"{entity}.{column}: only on one side"
            for column in sorted(left_cols ^ right_cols)
        )
        for column in sorted(left_cols & right_cols):
            if str(left[column].dtype) != str(right[column].dtype):
                differences.append(f"{entity}.{column}: dtype differs")
            elif not left[column].equals(right[column]):
                differences.append(f"{entity}.{column}: values differ")
    return DeterminismVerdict(equal=not differences, differences=differences[:50])


def publish_manifest_impl(bucket: str, plan: PrecomputePlan) -> str:
    """Store the plan's manifest write-once and return its content digest."""
    from policyengine_simulation_executor.artifact_store import (  # noqa: PLC0415  # resolved per call so tests can patch artifact_store.ArtifactStore
        ArtifactStore,
    )

    manifest = build_manifest(plan)
    return ArtifactStore(bucket).write_manifest(manifest.canonical_payload())


@dataclass(frozen=True)
class PrecomputeFunctions:
    """The six Modal function handles the precompute orchestration calls."""

    plan_artifacts: RemoteFunction
    build_dataset: RemoteFunction
    build_state_datasets: RemoteFunction
    compute_baseline: RemoteFunction
    verify_determinism: RemoteFunction
    publish_manifest: RemoteFunction


def run_precompute(
    bucket: str,
    *,
    force: bool,
    functions: PrecomputeFunctions,
    echo: Callable[[str], None] = print,
) -> str:
    """Orchestrate the local entrypoint: plan, spawn misses, gate, publish.

    Takes the six Modal function handles as ``functions`` (each offering
    ``.remote``/``.spawn``) so the flow is testable with plain fakes.
    Per-state dataset builds (one container per year) start with the dataset
    builds and are waited for only before the manifest is published, so the
    baselines never wait for them.
    Everything crossing a handle is a plain dict (Modal's serialization
    boundary); it is validated back into models immediately on return.
    The determinism gate runs only when baselines were actually computed
    (a no-op run must stay a no-op). Returns the manifest digest; the
    LAST echoed line is the ``MANIFEST_DIGEST=`` contract the deploy job
    parses.
    """
    echo(f"Planning against gs://{bucket} (force={force})")
    plan = PrecomputePlan.model_validate(functions.plan_artifacts.remote(bucket))
    work = select_work(plan, force=force)
    echo(
        f"Plan: {len(plan.datasets)} datasets ({len(work.datasets)} to "
        f"build), {len(plan.state_datasets)} state datasets "
        f"({len(work.state_datasets)} to build), {len(plan.baselines)} baselines "
        f"({len(work.baselines)} to compute)"
    )

    dataset_handles = [
        functions.build_dataset.spawn(bucket, entry.model_dump())
        for entry in work.datasets
    ]
    # One build prepares one dataset's year file, so builds are per year and
    # dataset.
    state_builds: dict[tuple[int, str], list[dict]] = {}
    for entry in work.state_datasets:
        state_builds.setdefault((entry.year, entry.dataset), []).append(
            entry.model_dump()
        )
    state_handles = [
        (year, dataset, functions.build_state_datasets.spawn(bucket, year, entries))
        for (year, dataset), entries in state_builds.items()
    ]
    for handle in dataset_handles:
        result = DatasetBuildResult.model_validate(handle.get())
        echo(
            f"dataset year={result.year} built in "
            f"{result.build_seconds}s ({result.size_bytes} bytes, "
            f"uploaded={result.uploaded})"
        )

    baseline_handles = [
        functions.compute_baseline.spawn(bucket, entry.model_dump())
        for entry in work.baselines
    ]
    for handle in baseline_handles:
        result = BaselineComputeResult.model_validate(handle.get())
        echo(
            f"baseline year={result.year} id={result.simulation_id} "
            f"outcome={result.outcome} in {result.compute_seconds}s "
            f"(uploaded={result.uploaded})"
        )

    if work.baselines:
        probe = work.baselines[0]
        echo(f"Determinism gate: recomputing year={probe.year} group={probe.group}")
        verdict = DeterminismVerdict.model_validate(
            functions.verify_determinism.remote(bucket, probe.model_dump())
        )
        if not verdict.equal:
            raise SystemExit(
                "Determinism gate FAILED: " + "; ".join(verdict.differences)
            )
        echo("Determinism gate passed")

    for year, dataset, handle in state_handles:
        results = [
            StateDatasetBuildResult.model_validate(result) for result in handle.get()
        ]
        uploaded = [result for result in results if result.uploaded]
        echo(
            f"State datasets {year} {dataset}: {len(results)} built, "
            f"{len(uploaded)} uploaded "
            f"({sum(result.size_bytes for result in uploaded)} bytes) in "
            f"{max((result.build_seconds for result in results), default=0.0)} s"
        )

    manifest_digest = functions.publish_manifest.remote(bucket, plan.model_dump())
    echo(manifest_digest_line(manifest_digest))
    return manifest_digest
