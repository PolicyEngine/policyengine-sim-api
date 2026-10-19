"""Validated Stage 12 cache loading with calculation fallback."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from policyengine.core import Simulation
from policyengine.core.scoping_strategy import RegionGroupStrategy
from policyengine.core.simulation import _cache
from policyengine_simulation_contract.stage12_execution import (
    PlannedSimulationExecutionInput,
    SimulationRole,
    Stage12OutputPlan,
)
from pydantic import PrivateAttr

from policyengine_simulation_executor.stage12_cache.keys import (
    Stage12BaselineCacheIdentity,
    collect_baseline_identity,
)

from .output_planning import validate_output_frames

if TYPE_CHECKING:
    from collections.abc import Mapping

    from policyengine.core.scoping_strategy import ScopingStrategy
    from policyengine_simulation_contract.stage12_bundle import ResolvedStage12Bundle

logger = logging.getLogger(__name__)

CACHE_HIT = "hit"
CACHE_INCOMPLETE = "incomplete"
CACHE_MISS = "miss"


def qualifying_cache_identity(
    simulation: PlannedSimulationExecutionInput,
    *,
    resolved: ResolvedStage12Bundle,
    dataset_is_default: bool,
    scoping_strategy: ScopingStrategy | None,
    spm: Mapping[str, Any] | None,
) -> Stage12BaselineCacheIdentity | None:
    """Return a safe cache identity, or None for an ineligible simulation."""
    if not _is_current_law_default_baseline(
        simulation, dataset_is_default=dataset_is_default
    ):
        return None
    region_codes = _state_region_group(simulation)
    scope_key = _region_group_cache_key(scoping_strategy)
    if region_codes is None or scope_key is None:
        return None
    return collect_baseline_identity(
        simulation,
        resolved=resolved,
        region_codes=region_codes,
        scope_key=scope_key,
        spm=spm,
    )


def _is_current_law_default_baseline(
    simulation: PlannedSimulationExecutionInput, *, dataset_is_default: bool
) -> bool:
    """Whether a simulation is a baseline with no reform on the default dataset."""
    return (
        simulation.role is SimulationRole.BASELINE
        and not simulation.policy
        and dataset_is_default
    )


def _state_region_group(
    simulation: PlannedSimulationExecutionInput,
) -> tuple[str, ...] | None:
    """Return the simulation's non-empty group of state regions, or None."""
    raw_group = simulation.options.get("region_group")
    if not isinstance(raw_group, list) or not raw_group:
        return None
    if any(
        not isinstance(code, str) or not code.startswith("state/") for code in raw_group
    ):
        return None
    return tuple(code for code in raw_group if isinstance(code, str))


def _region_group_cache_key(scoping_strategy: ScopingStrategy | None) -> str | None:
    """Return a region-group scope's cache key, or None when it has none."""
    if not isinstance(scoping_strategy, RegionGroupStrategy):
        return None
    scope_key = getattr(scoping_strategy, "cache_key", None)
    if not isinstance(scope_key, str) or not scope_key:
        return None
    return scope_key


class Stage12CachedSimulation(Simulation):
    """A deterministic baseline that validates any loaded Stage 12 cache."""

    _stage12_output_plan: Stage12OutputPlan | None = PrivateAttr(default=None)
    _computed_this_process: bool = PrivateAttr(default=False)
    _stage12_cache_outcome: str | None = PrivateAttr(default=None)

    @property
    def stage12_cache_outcome(self) -> str | None:
        """The first cache outcome of ``ensure``: hit, incomplete or miss."""
        return self._stage12_cache_outcome

    def configure_stage12_cache(self, output_plan: Stage12OutputPlan) -> None:
        """Set the output plan a loaded cache file must satisfy."""
        self._stage12_output_plan = output_plan

    def run(self) -> None:
        """Calculate the simulation, noting that this process computed it."""
        self._computed_this_process = True
        super().run()

    def _record_outcome(self, outcome: str) -> None:
        if self._stage12_cache_outcome is None:
            self._stage12_cache_outcome = outcome

    def ensure(self) -> None:
        """Load or calculate the output, then check any loaded file.

        A file that fails the output plan is recalculated and saved again, and
        .py's in-memory cache is cleared, because it still holds the incomplete
        output and offers no way to replace one entry. The next ``ensure`` then
        loads the saved file. Raises ``RuntimeError`` when a file was loaded but
        no output plan was configured.
        """
        self._computed_this_process = False
        super().ensure()
        if self._computed_this_process:
            self._record_outcome(CACHE_MISS)
            return
        plan = self._stage12_output_plan
        if plan is None:
            msg = "Stage 12 cached simulation has no output plan"
            raise RuntimeError(msg)
        data = getattr(getattr(self, "output_dataset", None), "data", None)
        entity_data = getattr(data, "entity_data", None)
        try:
            validate_output_frames(entity_data or {}, plan)
        except (TypeError, ValueError):
            logger.warning(
                "Stage 12 cache %s is incomplete; recalculating",
                self.id,
            )
            self._record_outcome(CACHE_INCOMPLETE)
            self.run()
            self.save()
            _cache.clear()
            return
        self._record_outcome(CACHE_HIT)
