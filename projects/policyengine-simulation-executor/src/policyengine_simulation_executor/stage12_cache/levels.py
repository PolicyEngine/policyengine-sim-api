"""Prebuilt year files and baselines, by country and dataset level.

A dataset level names one kind of run by the households it simulates: a US
national, group-of-states, state or congressional-district run, or a UK
national, nation or local-area run. One handler per country and level says
which year files and current-law baselines the precompute builds for a year,
how to write those year files, which year file a run reads, which prebuilt
folder holds it, and whether the run's current-law baseline gets a fixed ID.
Each country's settings say which years and levels are precomputed.

The handlers never build a key, touch the store or read a manifest: each app
turns a ``YearFileTarget`` into its own identity. The run_simulation app
imports these modules; Stage 12 modules must not import the run_simulation
feature modules (``tests/test_stage12_segmentation_foundation.py``), so
nothing here does either.

Every prebuilt file lands under the image data folder at
``prebuilt/<country>/<dataset>/<subset code or "all">/``: year files as
``<stem>_year_<year>.h5`` and baselines beside the year file they were
computed on, which is where .py's ``Simulation.ensure()`` looks for them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from pathlib import Path

    from policyengine.core import Simulation
    from policyengine.core.scoping_strategy import RowFilterStrategy

    from policyengine_simulation_executor.region_resolution import RegionResolution
    from policyengine_simulation_executor.simulation_runtime import DatasetSelection

logger = logging.getLogger(__name__)

PREBUILT_DIRECTORY = "prebuilt"
WHOLE_DATASET_FOLDER = "all"
# Values of the run's ``dataset_source`` attribute when its region loads a
# non-default dataset: read from the image's prebuilt file, or prepared from
# the full dataset during the request.
BAKED_STATE_DATASET_SOURCE = "baked_state"
ON_DEMAND_DATASET_SOURCE = "on_demand"


class DatasetLevel(StrEnum):
    """The kinds of run a prebuilt file can serve, named by the households simulated."""

    NATIONAL = "national"
    STATE_GROUP = "state_group"
    STATE = "state"
    DISTRICT = "district"
    NATION = "nation"
    LOCAL_AREA = "local_area"


def prebuilt_folder(country: str, dataset: str, subset_code: str | None = None) -> str:
    """Return a prebuilt folder relative to the image data folder."""
    return (
        f"{PREBUILT_DIRECTORY}/{country.lower()}/{dataset}/"
        f"{subset_code or WHOLE_DATASET_FOLDER}"
    )


@dataclass(frozen=True)
class SubsetFilter:
    """The households a subset's year file keeps: rows where ``variable`` is ``value``.

    ``code`` names the subset's image folder, such as ``CA``.
    """

    region: str
    code: str
    variable: str
    value: int
    scoping_strategy: RowFilterStrategy = field(compare=False, repr=False)


@dataclass(frozen=True)
class YearFileTarget:
    """One year file a level owns: a dataset's whole year, or one subset's rows of it.

    ``folder`` is where the image keeps it, relative to the data folder.
    """

    country: str
    level: DatasetLevel
    dataset: str
    year: int
    subset: SubsetFilter | None
    folder: str


@dataclass(frozen=True)
class RunScope:
    """The resolved facts of one run that the handlers look at."""

    country: str
    year: int
    region: RegionResolution
    dataset: DatasetSelection


@dataclass(frozen=True)
class BaselineScope:
    """The region label and scoping key a fixed baseline ID is computed from.

    ``scope_key`` is the scoping strategy's ``cache_key``; None for a run of a
    whole dataset.
    """

    region: str
    scope_key: str | None


@dataclass(frozen=True)
class BaselineTarget:
    """One current-law baseline the precompute builds.

    ``request`` is the request the runtime would receive for it, with scope
    ``macro`` and no policy; ``year_file`` is the file it runs on, and so the
    folder it lands in.
    """

    country: str
    level: DatasetLevel
    year: int
    request: Mapping[str, Any] = field(compare=False)
    year_file: YearFileTarget
    label: str


@dataclass(frozen=True)
class SourceFileTarget:
    """A bundle dataset the image holds as is, in ``folder``."""

    country: str
    level: DatasetLevel
    dataset: str
    folder: str


@dataclass(frozen=True)
class PrebuiltFolder:
    """The prebuilt folder a run reads, and the ``dataset_source`` it reports.

    ``dataset_source`` is None for a run of the default dataset, which
    reports none.
    """

    folder: str
    dataset_source: str | None


class LevelHandler(Protocol):
    """What one country and dataset level builds, and what its runs read."""

    @property
    def country(self) -> str:
        """The country, such as ``us``."""
        ...

    @property
    def level(self) -> DatasetLevel:
        """The dataset level this handler covers."""
        ...

    @property
    def baked_dataset_source(self) -> str | None:
        """The ``dataset_source`` a run reports when it reads a prebuilt file."""
        ...

    def year_file_targets(self, year: int) -> Sequence[YearFileTarget]:
        """Return the year files this level owns for ``year``."""
        ...

    def baseline_targets(self, year: int) -> Sequence[BaselineTarget]:
        """Return the current-law baselines this level precomputes for ``year``."""
        ...

    def source_file_targets(self) -> Sequence[SourceFileTarget]:
        """Return the bundle datasets this level copies into the image."""
        ...

    def write_year_files(
        self, year: int, targets: Sequence[YearFileTarget], work_folder: Path
    ) -> Mapping[YearFileTarget, Path]:
        """Write ``targets`` (all this level's, for ``year``) and return each file."""
        ...

    def year_file_for(self, run: RunScope) -> YearFileTarget | None:
        """Return the year file ``run`` reads, or None when none applies."""
        ...

    def baked_data_folder(self, run: RunScope, data_folder: str) -> str | None:
        """Return the prebuilt folder holding ``run``'s usable year file, or None."""
        ...

    def baseline_scope(self, run: RunScope) -> BaselineScope | None:
        """Return the scope of ``run``'s fixed baseline ID, or None for a random ID."""
        ...


@dataclass(frozen=True)
class CountryPrebuilt:
    """One country's handlers and the years and levels its precompute builds.

    ``years`` are in submission order. ``classify`` names a run's level and
    ``baseline_output_preparer`` adds the output variables a precomputed
    baseline must carry.
    """

    country: str
    years: tuple[int, ...]
    year_file_levels: tuple[DatasetLevel, ...]
    baseline_levels: tuple[DatasetLevel, ...]
    handlers: Mapping[DatasetLevel, LevelHandler]
    classify: Callable[[RunScope], DatasetLevel | None] = field(repr=False)
    baseline_output_preparer: Callable[[Simulation], None] = field(repr=False)

    def level_of(self, run: RunScope) -> DatasetLevel | None:
        """Return ``run``'s level, or None when no handler covers it.

        Never raises: it runs on the request path, where a failure to
        classify means only that nothing prebuilt applies.
        """
        try:
            level = self.classify(run)
        except Exception:  # noqa: BLE001  # the request path never fails over a prebuilt-file lookup; the failure is logged
            logger.warning(
                "Could not name the dataset level of %s region %s; reading no "
                "prebuilt file",
                run.country,
                run.region.code,
                exc_info=True,
            )
            return None
        return level if level in self.handlers else None

    def handler_for_run(self, run: RunScope) -> LevelHandler | None:
        """Return the handler of ``run``'s level, or None."""
        level = self.level_of(run)
        return None if level is None else self.handlers[level]

    def prepare_baseline_outputs(self, baseline: Simulation) -> None:
        """Add the output variables a precomputed baseline must carry."""
        self.baseline_output_preparer(baseline)


def prebuilt_for(country: str) -> CountryPrebuilt | None:
    """Return a country's prebuilt-file settings, or None for another country."""
    normalized = country.lower()
    if normalized == "us":
        from policyengine_simulation_executor.stage12_cache import (  # noqa: PLC0415  # the US handlers load the US registry helpers; loaded per call
            levels_us,
        )

        return levels_us.us_prebuilt()
    if normalized == "uk":
        from policyengine_simulation_executor.stage12_cache import (  # noqa: PLC0415  # the UK handlers load the UK metadata helpers; loaded per call
            levels_uk,
        )

        return levels_uk.uk_prebuilt()
    return None


def handler_for(country: str, level: DatasetLevel) -> LevelHandler:
    """Return one country's handler for ``level``; ``KeyError`` when it has none."""
    prebuilt = prebuilt_for(country)
    if prebuilt is None:
        raise KeyError(country)
    return prebuilt.handlers[level]


def year_file_name(target: YearFileTarget) -> str:
    """Return the basename .py's ``ensure_datasets`` resolves for a year file."""
    from policyengine.provenance.manifest import (
        dataset_logical_name,
        resolve_dataset_reference,
    )

    stem = dataset_logical_name(
        resolve_dataset_reference(target.country, target.dataset)
    )
    return f"{stem}_year_{target.year}.h5"


def prebuilt_dataset_folder(run: RunScope, data_folder: str) -> PrebuiltFolder | None:
    """Return the prebuilt folder ``run`` reads its year file from, or None.

    None, for a run no handler covers, a year file the image lacks or one .py
    would not reuse, keeps today's folders: the data folder's root for the
    default dataset and a temporary folder for any other. Never raises, so a
    lookup failure costs a dataset build, never a request.
    """
    prebuilt = prebuilt_for(run.country)
    if prebuilt is None:
        return None
    handler = prebuilt.handler_for_run(run)
    if handler is None:
        return None
    try:
        folder = handler.baked_data_folder(run, data_folder)
    except Exception:  # noqa: BLE001  # the request path never fails over a prebuilt-file lookup; the failure is logged
        logger.warning(
            "Could not look up the prebuilt year file of %s region %s; "
            "preparing the dataset instead",
            run.country,
            run.region.code,
            exc_info=True,
        )
        return None
    if folder is None:
        return None
    return PrebuiltFolder(folder=folder, dataset_source=handler.baked_dataset_source)
