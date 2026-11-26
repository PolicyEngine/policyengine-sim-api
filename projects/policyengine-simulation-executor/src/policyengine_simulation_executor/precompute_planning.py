"""Plan one country's precompute through its level handlers.

The country's settings (``stage12_cache.levels.prebuilt_for``) name the years
and the levels whose year files and baselines are built. Each level's handler
returns its targets for a year; this module keys each target with the
run_simulation app's identities and checks the store for it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from policyengine_simulation_executor.precompute_models import (
    BaselinePlanEntry,
    BundleVersionIdentity,
    PrecomputePlan,
    SourceFilePlanEntry,
    WorkSelection,
    YearFilePlanEntry,
)
from policyengine_simulation_executor.stage12_cache.levels import (
    BaselineTarget,
    CountryPrebuilt,
    prebuilt_for,
)

if TYPE_CHECKING:
    from policyengine_simulation_executor.artifact_keys import BaselineIdentity
    from policyengine_simulation_executor.artifact_store import ArtifactStore


def country_prebuilt(country: str) -> CountryPrebuilt:
    """Return a country's prebuilt-file settings; ``ValueError`` when it has none."""
    prebuilt = prebuilt_for(country)
    if prebuilt is None:
        msg = f"No prebuilt-file settings for country {country!r}"
        raise ValueError(msg)
    return prebuilt


def baseline_target_identity(target: BaselineTarget) -> BaselineIdentity | None:
    """Return a baseline target's identity, derived as a run of its request is.

    The request goes through the runtime's own region and dataset resolution
    and ``qualifying_baseline_identity``, so the writer and the reader cannot
    disagree by construction.
    """
    from policyengine_simulation_executor.baseline_artifacts import (
        BaselineIdentityRequest,
        qualifying_baseline_identity,
    )
    from policyengine_simulation_executor.simulation_runtime import (  # noqa: PLC0415  # resolved per call so tests can patch simulation_runtime helpers
        _country_module,
        _resolve_dataset_selection,
        resolve_region,
    )

    params = dict(target.request)
    region = resolve_region(
        country_module=_country_module(target.country),
        country=target.country,
        params=params,
    )
    dataset = _resolve_dataset_selection(params, region_resolution=region)
    return qualifying_baseline_identity(
        BaselineIdentityRequest(
            params,
            country=target.country,
            dataset=dataset,
            policy=None,
            region_code=region.code,
            scoping_strategy=region.scoping_strategy,
            year=target.year,
            state_code=region.state_code,
        )
    )


def select_work(plan: PrecomputePlan, *, force: bool) -> WorkSelection:
    """Which plan entries need computing."""
    return WorkSelection(
        year_files=[entry for entry in plan.year_files if force or not entry.exists],
        baselines=[entry for entry in plan.baselines if force or not entry.exists],
        source_files=[
            entry for entry in plan.source_files if force or not entry.exists
        ],
    )


def _year_file_entries(
    prebuilt: CountryPrebuilt, year: int, store: ArtifactStore
) -> list[YearFilePlanEntry]:
    from policyengine_simulation_executor.artifact_keys import (  # noqa: PLC0415  # resolved per call so tests can patch artifact_keys
        year_file_identity,
    )

    entries = []
    for level in prebuilt.year_file_levels:
        for target in prebuilt.handlers[level].year_file_targets(year):
            identity = year_file_identity(target)
            entries.append(
                YearFilePlanEntry(
                    country=prebuilt.country,
                    level=target.level,
                    year=target.year,
                    dataset=target.dataset,
                    region=None if target.subset is None else target.subset.region,
                    subset_code=None if target.subset is None else target.subset.code,
                    digest=identity.digest,
                    path=identity.store_path,
                    filename=identity.filename,
                    folder=identity.folder,
                    exists=store.exists(identity.store_path),
                )
            )
    return entries


def _baseline_entries(
    prebuilt: CountryPrebuilt, year: int, store: ArtifactStore
) -> list[BaselinePlanEntry]:
    entries = []
    for level in prebuilt.baseline_levels:
        for target in prebuilt.handlers[level].baseline_targets(year):
            identity = baseline_target_identity(target)
            if identity is None:
                msg = (
                    f"The {target.country} {target.level} baseline {target.label} "
                    f"for {year} gets no fixed ID: the writer and reader rules "
                    "have diverged."
                )
                raise RuntimeError(msg)
            if identity.year_file.folder != target.year_file.folder:
                msg = (
                    f"The {target.label} baseline reads {identity.year_file.folder}, "
                    f"not its target's {target.year_file.folder}"
                )
                raise RuntimeError(msg)
            entries.append(
                BaselinePlanEntry(
                    country=prebuilt.country,
                    level=target.level,
                    year=target.year,
                    region=identity.region,
                    label=target.label,
                    request=dict(target.request),
                    year_file_path=identity.year_file.store_path,
                    year_file_filename=identity.year_file.filename,
                    folder=identity.year_file.folder,
                    digest=identity.digest,
                    path=identity.store_path,
                    simulation_id=identity.simulation_id,
                    storage_id=identity.storage_id,
                    exists=store.exists(identity.store_path),
                )
            )
    return entries


def _source_file_entries(
    prebuilt: CountryPrebuilt, store: ArtifactStore
) -> list[SourceFilePlanEntry]:
    from policyengine_simulation_executor.artifact_keys import (  # noqa: PLC0415  # resolved per call so tests can patch artifact_keys
        source_file_identity,
    )

    entries = []
    levels = dict.fromkeys((*prebuilt.year_file_levels, *prebuilt.baseline_levels))
    for level in levels:
        for target in prebuilt.handlers[level].source_file_targets():
            identity = source_file_identity(target)
            entries.append(
                SourceFilePlanEntry(
                    country=prebuilt.country,
                    level=target.level,
                    dataset=target.dataset,
                    digest=identity.digest,
                    sha256=identity.dataset_sha256,
                    path=identity.store_path,
                    filename=identity.filename,
                    folder=identity.folder,
                    exists=store.exists(identity.store_path),
                )
            )
    return entries


def _receipt(country: str) -> BundleVersionIdentity:
    from policyengine_simulation_executor import (  # noqa: PLC0415  # resolved per call so tests can patch artifact_keys._installed_package_versions
        artifact_keys,
    )
    from policyengine_simulation_executor.release_bundle import (  # noqa: PLC0415  # resolved per call so tests can patch release_bundle.get_country_release_bundle
        get_country_release_bundle,
    )

    bundle = get_country_release_bundle(country)
    packages = artifact_keys._installed_package_versions(country)  # noqa: SLF001  # the one installed-package reader, shared with the year-file keys
    return BundleVersionIdentity(
        policyengine_version=bundle.policyengine_version,
        policyengine_core_version=packages["policyengine-core"],
        model_version=bundle.model_version,
        data_version=bundle.data_version,
        data_artifact_revision=bundle.data_artifact_revision,
        default_dataset=bundle.default_dataset,
    )


def plan_artifacts_impl(bucket: str, country: str = "us") -> PrecomputePlan:
    """Compute every file one country's precompute builds, and its store presence.

    Year-major: for each year, the year files of each level in the settings'
    order, then the baselines of each level. Source files follow.
    """
    from policyengine_simulation_executor.artifact_store import (  # noqa: PLC0415  # resolved per call so tests can patch artifact_store.ArtifactStore
        ArtifactStore,
    )

    prebuilt = country_prebuilt(country)
    store = ArtifactStore(bucket)
    year_files: list[YearFilePlanEntry] = []
    baselines: list[BaselinePlanEntry] = []
    for year in prebuilt.years:
        year_files += _year_file_entries(prebuilt, year, store)
        baselines += _baseline_entries(prebuilt, year, store)
    return PrecomputePlan(
        country=prebuilt.country,
        year_files=year_files,
        baselines=baselines,
        source_files=_source_file_entries(prebuilt, store),
        receipt=_receipt(prebuilt.country),
    )
