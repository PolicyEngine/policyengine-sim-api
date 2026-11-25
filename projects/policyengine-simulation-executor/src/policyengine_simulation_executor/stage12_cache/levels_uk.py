"""The UK level handlers: national, nation and local-area runs.

Nothing UK is prebuilt yet, so every UK handler builds nothing and finds no
prebuilt folder. UK-wide runs keep the fixed baseline ID they have today; 2.2's
tasks add the UK years, year files and baselines through these handlers.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from policyengine_simulation_executor.stage12_cache.levels import (
    BaselineScope,
    BaselineTarget,
    CountryPrebuilt,
    DatasetLevel,
    RunScope,
    SourceFileTarget,
    YearFileTarget,
    prebuilt_folder,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from policyengine.core import Simulation

logger = logging.getLogger(__name__)

UK = "uk"
_NATION_PREFIX = "country/"


def _whole_dataset_year_file(
    level: DatasetLevel, year: int, dataset: str
) -> YearFileTarget:
    return YearFileTarget(
        country=UK,
        level=level,
        dataset=dataset,
        year=year,
        subset=None,
        folder=prebuilt_folder(UK, dataset),
    )


class _UkLevel:
    """What every UK level shares today: nothing built, nothing prebuilt."""

    country = UK
    level: DatasetLevel
    baked_dataset_source: str | None = None

    def year_file_targets(self, year: int) -> Sequence[YearFileTarget]:  # noqa: ARG002  # no UK year file is prebuilt yet
        """Return no year files."""
        return []

    def baseline_targets(self, year: int) -> Sequence[BaselineTarget]:  # noqa: ARG002  # no UK baseline is precomputed yet
        """Return no baselines."""
        return []

    def source_file_targets(self) -> Sequence[SourceFileTarget]:
        """Return no source files."""
        return []

    def write_year_files(
        self,
        year: int,  # noqa: ARG002  # the protocol's signature
        targets: Sequence[YearFileTarget],
        work_folder: Path,  # noqa: ARG002  # the protocol's signature
    ) -> Mapping[YearFileTarget, Path]:
        """Refuse targets: no UK year file is built yet."""
        if targets:
            msg = f"The UK {self.level} level builds no year files yet"
            raise ValueError(msg)
        return {}

    def year_file_for(self, run: RunScope) -> YearFileTarget | None:
        """Return the default dataset's year file, which UK and nation runs read."""
        if not run.dataset.is_default:
            return None
        return _whole_dataset_year_file(
            DatasetLevel.NATIONAL, run.year, run.dataset.name
        )

    def baked_data_folder(self, run: RunScope, data_folder: str) -> str | None:  # noqa: ARG002  # no UK file is prebuilt yet
        """Return no folder: no UK file is prebuilt."""
        return None

    def baseline_scope(self, run: RunScope) -> BaselineScope | None:  # noqa: ARG002  # overridden by the national level
        """Return no scope: these baselines get random IDs."""
        return None


class UkNationalHandler(_UkLevel):
    """The UK-wide run on the default dataset."""

    level = DatasetLevel.NATIONAL

    def baseline_scope(self, run: RunScope) -> BaselineScope | None:
        """Give the unscoped UK-wide run on the default dataset a fixed ID."""
        if (
            run.region.code == UK
            and run.region.scoping_strategy is None
            and run.dataset.is_default
        ):
            return BaselineScope(region="national", scope_key=None)
        return None


class UkNationHandler(_UkLevel):
    """A run of one of the four nations, a row filter on the default dataset."""

    level = DatasetLevel.NATION


class UkLocalAreaHandler(_UkLevel):
    """A run on the dataset the bundle routes UK area regions to."""

    level = DatasetLevel.LOCAL_AREA

    def year_file_for(self, run: RunScope) -> YearFileTarget | None:
        """Return the local-area dataset's whole year file."""
        if run.dataset.is_default:
            return None
        return _whole_dataset_year_file(self.level, run.year, run.dataset.name)


def _routed_area_dataset_name() -> str | None:
    from policyengine_simulation_executor import (  # noqa: PLC0415  # resolved per call so tests can patch the routing helper
        uk_local_authority_metadata,
    )

    try:
        return uk_local_authority_metadata.routed_uk_area_dataset_name()
    except ValueError:
        return None


def uk_level_of(run: RunScope) -> DatasetLevel | None:
    """Return a UK run's level: local area, national, nation, or None.

    The local-area dataset comes first, because a breakdown run's region is
    ``uk`` or a nation. A dataset other than the default that the bundle does
    not route area regions to has no level.
    """
    if not run.dataset.is_default:
        routed = _routed_area_dataset_name()
        return DatasetLevel.LOCAL_AREA if run.dataset.name == routed else None
    if run.region.code == UK:
        return DatasetLevel.NATIONAL
    if run.region.code.startswith(_NATION_PREFIX):
        return DatasetLevel.NATION
    return None


def _prepare_uk_baseline_outputs(baseline: Simulation) -> None:
    """Add nothing: UK analyses request no extra baseline outputs."""


def uk_prebuilt() -> CountryPrebuilt:
    """Return the UK settings: no prebuilt files yet, and the UK-wide fixed ID."""
    handlers = (UkNationalHandler(), UkNationHandler(), UkLocalAreaHandler())
    return CountryPrebuilt(
        country=UK,
        years=(),
        year_file_levels=(),
        baseline_levels=(),
        handlers={handler.level: handler for handler in handlers},
        classify=uk_level_of,
        baseline_output_preparer=_prepare_uk_baseline_outputs,
    )
