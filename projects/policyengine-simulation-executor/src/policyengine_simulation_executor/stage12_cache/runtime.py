"""Validated Stage 12 cache loading with calculation fallback."""

from __future__ import annotations

import logging
from typing import Any

from policyengine.core import Simulation
from policyengine.core.scoping_strategy import RegionGroupStrategy
from policyengine_simulation_contract.stage12_bundle import ResolvedStage12Bundle
from policyengine_simulation_contract.stage12_execution import (
    PlannedSimulationExecutionInput,
    SimulationRole,
    Stage12OutputPlan,
)
from pydantic import PrivateAttr

from policyengine_simulation_executor.stage12_runtime.output_planning import (
    validate_output_frames,
)

from .keys import Stage12BaselineCacheIdentity, collect_baseline_identity

logger = logging.getLogger(__name__)

CACHE_HIT = "hit"
CACHE_INCOMPLETE = "incomplete"
CACHE_MISS = "miss"


def qualifying_cache_identity(
    simulation: PlannedSimulationExecutionInput,
    *,
    resolved: ResolvedStage12Bundle,
    dataset_is_default: bool,
    scoping_strategy: Any,
) -> Stage12BaselineCacheIdentity | None:
    """Return a safe cache identity, or None for an ineligible simulation."""

    if simulation.role is not SimulationRole.BASELINE:
        return None
    if simulation.policy:
        return None
    if not dataset_is_default:
        return None
    if not isinstance(scoping_strategy, RegionGroupStrategy):
        return None
    raw_group = simulation.options.get("region_group")
    if not isinstance(raw_group, list) or not raw_group:
        return None
    if any(
        not isinstance(code, str) or not code.startswith("state/") for code in raw_group
    ):
        return None
    scope_key = getattr(scoping_strategy, "cache_key", None)
    if not isinstance(scope_key, str) or not scope_key:
        return None
    return collect_baseline_identity(
        simulation,
        resolved=resolved,
        region_codes=tuple(raw_group),
        scope_key=scope_key,
    )


class Stage12CachedSimulation(Simulation):
    """A deterministic baseline that validates any loaded Stage 12 cache."""

    _stage12_output_plan: Stage12OutputPlan | None = PrivateAttr(default=None)
    _computed_this_process: bool = PrivateAttr(default=False)
    _stage12_cache_outcome: str | None = PrivateAttr(default=None)

    @property
    def stage12_cache_outcome(self) -> str | None:
        return self._stage12_cache_outcome

    def configure_stage12_cache(self, output_plan: Stage12OutputPlan) -> None:
        self._stage12_output_plan = output_plan

    def run(self) -> None:
        self._computed_this_process = True
        super().run()

    def _record_outcome(self, outcome: str) -> None:
        if self._stage12_cache_outcome is None:
            self._stage12_cache_outcome = outcome

    def ensure(self) -> None:
        self._computed_this_process = False
        super().ensure()
        if self._computed_this_process:
            self._record_outcome(CACHE_MISS)
            return
        plan = self._stage12_output_plan
        if plan is None:
            raise RuntimeError("Stage 12 cached simulation has no output plan")
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
            from policyengine.core.simulation import _cache

            storage_id = getattr(self, "storage_id", self.id)
            _cache._cache.pop(storage_id, None)
            _cache.add(storage_id, self)
            return
        self._record_outcome(CACHE_HIT)
