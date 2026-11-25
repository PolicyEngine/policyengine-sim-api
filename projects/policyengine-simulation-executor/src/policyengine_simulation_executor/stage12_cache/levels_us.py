"""The US level handlers: national, group-of-states, state and district runs.

The US precompute builds the default dataset's year file and each state's
rows of the state-level dataset (ACS-local) for 2026, 2027 and 2025, and the
current-law baseline of each of the 20 groups of states a segmented
nationwide run splits into. A national, group or state run reads its year
file from its prebuilt folder; a district reads its state's file.
"""

from __future__ import annotations

import gc
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from policyengine.core.scoping_strategy import RegionGroupStrategy, RowFilterStrategy

from policyengine_simulation_executor.stage12_cache.levels import (
    BAKED_STATE_DATASET_SOURCE,
    BaselineScope,
    BaselineTarget,
    CountryPrebuilt,
    DatasetLevel,
    RunScope,
    SourceFileTarget,
    SubsetFilter,
    YearFileTarget,
    prebuilt_folder,
    year_file_name,
)
from policyengine_simulation_executor.stage12_cache.state_datasets import (
    year_file_reuse_problem,
)
from policyengine_simulation_executor.stage12_runtime.partition import (
    US_REGION_GROUPS,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from types import ModuleType

    from microdf import MicroDataFrame
    from policyengine.core import Simulation

logger = logging.getLogger(__name__)

US = "us"
# The user-facing priority order (2026 first); with parallel builds it only
# orders submission, but keep it intentional.
US_PREBUILT_YEARS = (2026, 2027, 2025)
_STATE_PREFIX = "state/"
_DISTRICT_PREFIX = "congressional_district/"


def _default_dataset() -> str:
    from policyengine_simulation_executor.release_bundle import (  # noqa: PLC0415  # resolved per call so tests can patch release_bundle.get_country_release_bundle
        get_country_release_bundle,
    )

    return get_country_release_bundle(US).default_dataset


def _us_country_module() -> ModuleType:
    from policyengine_simulation_executor.simulation_runtime import (  # noqa: PLC0415  # resolved per call so tests can patch simulation_runtime._country_module
        _country_module,
    )

    return _country_module(US)


def national_year_file(year: int, dataset: str | None = None) -> YearFileTarget:
    """Return the whole default dataset's year file for ``year``."""
    name = dataset or _default_dataset()
    return YearFileTarget(
        country=US,
        level=DatasetLevel.NATIONAL,
        dataset=name,
        year=year,
        subset=None,
        folder=prebuilt_folder(US, name),
    )


def _subset_from_row_filter(
    region: str, state_code: str, strategy: object
) -> SubsetFilter | None:
    if not isinstance(strategy, RowFilterStrategy):
        return None
    value = strategy.variable_value
    if not strategy.variable_name or value is None or isinstance(value, list):
        return None
    return SubsetFilter(
        region=region,
        code=state_code.upper(),
        variable=strategy.variable_name,
        value=int(value),
        scoping_strategy=strategy,
    )


def _state_year_file(year: int, dataset: str, subset: SubsetFilter) -> YearFileTarget:
    return YearFileTarget(
        country=US,
        level=DatasetLevel.STATE,
        dataset=dataset,
        year=year,
        subset=subset,
        folder=prebuilt_folder(US, dataset, subset.code),
    )


class _UsLevel:
    """What every US level shares: no targets by default, and the baked-folder rule."""

    country = US
    level: DatasetLevel
    baked_dataset_source: str | None = None

    def year_file_targets(self, year: int) -> Sequence[YearFileTarget]:  # noqa: ARG002  # a level that owns no year files
        """Return no year files: this level reads another level's."""
        return []

    def baseline_targets(self, year: int) -> Sequence[BaselineTarget]:  # noqa: ARG002  # a level without precomputed baselines
        """Return no baselines: none are precomputed at this level."""
        return []

    def source_file_targets(self) -> Sequence[SourceFileTarget]:
        """Return no source files: the US image copies none."""
        return []

    def write_year_files(
        self,
        year: int,  # noqa: ARG002  # the protocol's signature
        targets: Sequence[YearFileTarget],
        work_folder: Path,  # noqa: ARG002  # the protocol's signature
    ) -> Mapping[YearFileTarget, Path]:
        """Refuse targets: this level owns no year files."""
        if targets:
            msg = f"The US {self.level} level owns no year files"
            raise ValueError(msg)
        return {}

    def year_file_for(self, run: RunScope) -> YearFileTarget | None:  # noqa: ARG002  # overridden by every level
        """Return no year file."""
        return None

    def baked_data_folder(self, run: RunScope, data_folder: str) -> str | None:
        """Return the folder of ``run``'s year file if it is there and .py reuses it.

        A file that fails .py's reuse checks would make ``ensure_datasets``
        rebuild the whole dataset inside the prebuilt folder, so the run
        reads today's folders instead.
        """
        target = self.year_file_for(run)
        if target is None:
            return None
        folder = Path(data_folder) / target.folder
        year_file = folder / year_file_name(target)
        if not year_file.is_file():
            return None
        try:
            problem = year_file_reuse_problem(year_file, target.dataset)
        except ImportError:
            # The reuse checks read private .py helpers. If an upgrade moves
            # one, the request prepares its dataset instead of failing.
            logger.warning(
                "Not reading the prebuilt year file %s: .py's year-file reuse "
                "checks could not be imported",
                year_file,
            )
            return None
        if problem is not None:
            logger.warning(
                "Not reading the prebuilt year file %s: %s", year_file, problem
            )
            return None
        return str(folder)

    def baseline_scope(self, run: RunScope) -> BaselineScope | None:  # noqa: ARG002  # overridden by the levels with fixed baseline IDs
        """Return no scope: this level's baselines get random IDs."""
        return None


class UsNationalHandler(_UsLevel):
    """The unscoped nationwide run on the default dataset."""

    level = DatasetLevel.NATIONAL

    def year_file_targets(self, year: int) -> Sequence[YearFileTarget]:
        """Return the default dataset's year file."""
        return [national_year_file(year)]

    def write_year_files(
        self,
        year: int,
        targets: Sequence[YearFileTarget],
        work_folder: Path,  # noqa: ARG002  # the default dataset's source file sits in the image data folder
    ) -> Mapping[YearFileTarget, Path]:
        """Prepare each year file in the image data folder, beside its source file."""
        from policyengine_simulation_executor.simulation_runtime import (  # noqa: PLC0415  # resolved per call so tests can patch resolve_data_folder
            resolve_data_folder,
        )

        data_folder = resolve_data_folder()
        written: dict[YearFileTarget, Path] = {}
        for target in targets:
            if target.level != self.level or target.subset is not None:
                msg = f"Not a whole-dataset national year file: {target}"
                raise ValueError(msg)
            _us_country_module().ensure_datasets(
                datasets=[target.dataset], years=[year], data_folder=data_folder
            )
            year_file = Path(data_folder) / year_file_name(target)
            if not year_file.exists():
                msg = f"ensure_datasets produced no file at {year_file}"
                raise RuntimeError(msg)
            written[target] = year_file
        return written

    def year_file_for(self, run: RunScope) -> YearFileTarget | None:
        """Return the default dataset's year file."""
        if not run.dataset.is_default:
            return None
        return national_year_file(run.year, run.dataset.name)

    def baseline_scope(self, run: RunScope) -> BaselineScope | None:
        """Give the unscoped nationwide run on the default dataset a fixed ID."""
        if (
            run.region.code == US
            and run.region.scoping_strategy is None
            and run.dataset.is_default
        ):
            return BaselineScope(region="national", scope_key=None)
        return None


class UsStateGroupHandler(_UsLevel):
    """The 20 groups of states a segmented nationwide run splits into."""

    level = DatasetLevel.STATE_GROUP

    def baseline_targets(self, year: int) -> Sequence[BaselineTarget]:
        """Return each group's current-law baseline, on the national year file."""
        year_file = national_year_file(year)
        return [
            BaselineTarget(
                country=US,
                level=self.level,
                year=year,
                request={
                    "country": US,
                    "scope": "macro",
                    "region_group": list(group),
                    "time_period": year,
                },
                year_file=year_file,
                label=f"group {index} of {len(US_REGION_GROUPS)}",
            )
            for index, group in enumerate(US_REGION_GROUPS, start=1)
        ]

    def year_file_for(self, run: RunScope) -> YearFileTarget | None:
        """Return the national year file, which a group's rows are filtered from."""
        if not run.dataset.is_default:
            return None
        return national_year_file(run.year, run.dataset.name)

    def baseline_scope(self, run: RunScope) -> BaselineScope | None:
        """Give a group run on the default dataset a fixed ID."""
        strategy = run.region.scoping_strategy
        if isinstance(strategy, RegionGroupStrategy) and run.dataset.is_default:
            return BaselineScope(region=run.region.code, scope_key=strategy.cache_key)
        return None


class UsStateHandler(_UsLevel):
    """A single-state run: its state's rows of the state-level dataset."""

    level = DatasetLevel.STATE
    baked_dataset_source = BAKED_STATE_DATASET_SOURCE

    def year_file_targets(self, year: int) -> Sequence[YearFileTarget]:
        """Return each state's year file, for states whose runs load another dataset.

        Each state is resolved exactly as a request for it is
        (``resolve_region`` then ``_resolve_dataset_selection``), so a bundle
        that sends states back to the default dataset gets no state files.
        """
        from policyengine_simulation_executor.simulation_runtime import (  # noqa: PLC0415  # resolved per call so tests can patch simulation_runtime helpers
            _resolve_dataset_selection,
            resolve_region,
        )

        country_module = _us_country_module()
        targets = []
        for region in country_module.model.region_registry.get_by_type("state"):
            params = {"country": US, "region": region.code, "time_period": year}
            resolution = resolve_region(
                country_module=country_module, country=US, params=params
            )
            selection = _resolve_dataset_selection(params, region_resolution=resolution)
            if selection.is_default:
                continue
            subset = (
                _subset_from_row_filter(
                    resolution.code, resolution.state_code, resolution.scoping_strategy
                )
                if resolution.state_code
                else None
            )
            if subset is None:
                msg = (
                    f"State region {resolution.code} loads {selection.name} but has no "
                    "row filter and state code to build its state file from"
                )
                raise RuntimeError(msg)
            targets.append(_state_year_file(year, selection.name, subset))
        return sorted(
            targets,
            key=lambda target: target.subset.code if target.subset else "",
        )

    def write_year_files(
        self, year: int, targets: Sequence[YearFileTarget], work_folder: Path
    ) -> Mapping[YearFileTarget, Path]:
        """Write each state's rows of one dataset's year file, checked as .py reuses it.

        Prepares the dataset's full-country year file once, then for each
        target keeps the rows its region's own scoping strategy keeps, writes
        them with .py's ``save()``, copies the year-file stamp and confirms
        that .py's reuse checks accept the file.
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

        if not targets:
            return {}
        datasets = {target.dataset for target in targets}
        if len(datasets) > 1:
            msg = f"One state file build covers one dataset, not {sorted(datasets)}"
            raise ValueError(msg)
        for target in targets:
            if target.year != year or target.level != self.level or not target.subset:
                msg = f"Not a {year} state year file: {target}"
                raise ValueError(msg)
        (dataset,) = datasets
        source_folder = str(work_folder / "source")
        country_module = _us_country_module()

        # Preparing the year file runs a Microsimulation over every state,
        # whose objects hold reference cycles; release them before filtering
        # 51 copies.
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

        written: dict[YearFileTarget, Path] = {}
        for target in targets:
            subset = target.subset
            if subset is None:  # checked above; narrows the type
                continue
            rows = subset.scoping_strategy.apply(
                entity_data=source.data.entity_data,
                group_entities=US_GROUP_ENTITIES,
                year=year,
            )
            year_file = work_folder / target.folder / year_file_name(target)
            PolicyEngineUSDataset(
                name=f"{dataset}-{subset.code.lower()}-year-{year}",
                description=f"{subset.region} rows of {dataset} for {year}",
                filepath=str(year_file),
                year=year,
                metadata=dict(source.metadata),
                data=USYearData(**rows),
            ).save()
            # save() rewrites the file without the stamp, so stamp it
            # afterwards, as create_datasets does.
            write_year_file_stamp(year_file, stamp)
            problem = year_file_reuse_problem(year_file, dataset)
            if problem is not None:
                msg = f"ensure_datasets would not reuse {year_file}: {problem}"
                raise RuntimeError(msg)
            written[target] = year_file
        return written

    def year_file_for(self, run: RunScope) -> YearFileTarget | None:
        """Return the state's year file, or the national one on the default dataset."""
        if run.dataset.is_default:
            return national_year_file(run.year, run.dataset.name)
        if not run.region.state_code:
            return None
        subset = _subset_from_row_filter(
            run.region.code, run.region.state_code, run.region.scoping_strategy
        )
        if subset is None:
            return None
        return _state_year_file(run.year, run.dataset.name, subset)


class UsDistrictHandler(_UsLevel):
    """A congressional-district run: its state's year file, filtered at run time."""

    level = DatasetLevel.DISTRICT
    baked_dataset_source = BAKED_STATE_DATASET_SOURCE

    def year_file_for(self, run: RunScope) -> YearFileTarget | None:
        """Return the district's state's year file (the national one on the default)."""
        if run.dataset.is_default:
            return national_year_file(run.year, run.dataset.name)
        state_code = run.region.state_code
        if not state_code:
            return None
        state_region = f"{_STATE_PREFIX}{state_code.lower()}"
        region = _us_country_module().model.get_region(state_region)
        subset = _subset_from_row_filter(
            state_region, state_code, getattr(region, "scoping_strategy", None)
        )
        if subset is None:
            return None
        return _state_year_file(run.year, run.dataset.name, subset)


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
    in_another_state = district.isin(sorted(district_geoids)) & (
        (district // 100) != household["state_fips"]
    )
    if in_another_state.any():
        msg = (
            "Some households have a congressional district code that names "
            "another state's district; their state's file would not match "
            "that district's results"
        )
        raise RuntimeError(msg)


def us_level_of(run: RunScope) -> DatasetLevel | None:
    """Return a US run's level: group, national, state, district, or None."""
    if isinstance(run.region.scoping_strategy, RegionGroupStrategy):
        return DatasetLevel.STATE_GROUP
    code = run.region.code
    if code == US:
        return DatasetLevel.NATIONAL
    if code.startswith(_STATE_PREFIX):
        return DatasetLevel.STATE
    if code.startswith(_DISTRICT_PREFIX):
        return DatasetLevel.DISTRICT
    return None


def _prepare_us_baseline_outputs(baseline: Simulation) -> None:
    # The extras economic_impact_analysis applies unconditionally before
    # ensure(); a precomputed baseline must carry them, or every request
    # would fail the column check and recompute.
    from policyengine.tax_benefit_models.us.analysis import (
        configure_budgetary_impact_variables,
    )

    configure_budgetary_impact_variables(baseline, baseline)


def us_prebuilt() -> CountryPrebuilt:
    """Return the US settings: today's files, through the four US handlers."""
    handlers = (
        UsNationalHandler(),
        UsStateGroupHandler(),
        UsStateHandler(),
        UsDistrictHandler(),
    )
    return CountryPrebuilt(
        country=US,
        years=US_PREBUILT_YEARS,
        year_file_levels=(DatasetLevel.NATIONAL, DatasetLevel.STATE),
        baseline_levels=(DatasetLevel.STATE_GROUP,),
        handlers={handler.level: handler for handler in handlers},
        classify=us_level_of,
        baseline_output_preparer=_prepare_us_baseline_outputs,
    )
