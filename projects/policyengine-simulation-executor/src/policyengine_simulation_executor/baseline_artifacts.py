"""Deterministic baseline simulation ids and the validate-on-load guard.

The runtime read path of the artifact pipeline. A baseline simulation whose
identity is fully determined by the installed version-set (current-law
policy, default data, national or region-group scope) gets a deterministic
``Simulation.id`` from ``artifact_keys``; ``Simulation.ensure()`` then finds
``{id}.h5`` beside the dataset when a precomputed artifact was baked into
the image, and loads in seconds instead of running the model.

Safety inversion: a load is trusted only after validation. Requests can
demand output columns beyond what an artifact was built with (cliff
variables, labor-supply-response columns), and ``ensure()`` alone would
serve the stale column set. ``ArtifactBaselineSimulation`` therefore diffs
``resolve_entity_variables`` against the loaded frames and falls back to
``run()`` on any gap — so a missing or incomplete artifact can only ever
cost compute, never correctness. With no artifact present this class
behaves exactly like ``Simulation`` (FileNotFoundError -> run + save),
which is what makes it safe to ship before any artifacts exist.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from pydantic import PrivateAttr

from policyengine.core import Simulation

from policyengine_simulation_executor import artifact_keys

logger = logging.getLogger(__name__)

# Artifact outcomes reported via the `baseline_artifact` observability
# attribute. "hit": loaded (disk or in-process cache) and complete;
# "incomplete": loaded but missing requested columns -> recomputed;
# "miss": no artifact -> computed (today's behavior).
OUTCOME_HIT = "hit"
OUTCOME_INCOMPLETE = "incomplete"
OUTCOME_MISS = "miss"


def qualifying_baseline_identity(
    params: dict[str, Any],
    *,
    country: str,
    policy: Any,
    region_code: Optional[str],
    scoping_strategy: Any,
    year: int,
) -> Optional[artifact_keys.BaselineArtifactIdentity]:
    """Full artifact identity for a qualifying baseline, else None.

    Qualifying means the simulation's bytes are a pure function of the
    installed version-set: current-law policy, macro scope, default data
    (custom ``data``/``data_version`` requests resolve different datasets
    AND live outside the baked folder), and a scope the pipeline produces
    artifacts for — unscoped national or a region group. Single states stay
    on random ids in v1.

    Both the runtime reader (via ``deterministic_baseline_id``) and the
    precompute writer derive identity through THIS function, so writer and
    reader cannot disagree by construction. Identity-collection errors
    propagate: the writer must fail loudly on them.
    """
    from policyengine.core.scoping_strategy import RegionGroupStrategy

    if policy is not None:
        return None
    if str(params.get("scope") or "").lower() != "macro":
        return None
    if params.get("data") is not None or params.get("data_version") is not None:
        return None
    if region_code is None:
        return None

    country = country.lower()
    if scoping_strategy is None:
        if region_code != country:
            return None
        region, scope_key = artifact_keys.NATIONAL_REGION, None
    elif isinstance(scoping_strategy, RegionGroupStrategy):
        region, scope_key = region_code, scoping_strategy.cache_key
    else:
        return None

    return artifact_keys.collect_baseline_identity(
        country, year, region=region, scope_key=scope_key
    )


def deterministic_baseline_id(
    params: dict[str, Any],
    *,
    country: str,
    policy: Any,
    region_code: Optional[str],
    scoping_strategy: Any,
    year: int,
) -> Optional[str]:
    """The runtime wrapper: id for a qualifying baseline, else None.

    Unlike the writer, the read path must never fail a request over
    identity collection (a manifest/receipt hiccup) — it degrades to a
    random id, meaning no artifact reuse for that request.
    """
    try:
        identity = qualifying_baseline_identity(
            params,
            country=country,
            policy=policy,
            region_code=region_code,
            scoping_strategy=scoping_strategy,
            year=year,
        )
    except Exception:
        logger.warning(
            "Could not collect baseline artifact identity for %s; "
            "using a random simulation id",
            country,
            exc_info=True,
        )
        return None
    return None if identity is None else identity.simulation_id


class ArtifactBaselineSimulation(Simulation):
    """A Simulation whose ``ensure()`` validates what it loads."""

    _computed_this_process: bool = PrivateAttr(default=False)
    _artifact_outcome: Optional[str] = PrivateAttr(default=None)

    @property
    def artifact_outcome(self) -> Optional[str]:
        """Set by ``ensure()``; None until then."""
        return self._artifact_outcome

    def run(self):
        self._computed_this_process = True
        super().run()

    def ensure(self) -> None:
        self._computed_this_process = False
        super().ensure()
        if self._computed_this_process:
            self._artifact_outcome = OUTCOME_MISS
            return

        missing = self._missing_output_columns()
        if not missing:
            self._artifact_outcome = OUTCOME_HIT
            return

        logger.warning(
            "Baseline artifact %s is missing output columns %s; recomputing",
            self.id,
            missing,
        )
        self._artifact_outcome = OUTCOME_INCOMPLETE
        self.run()
        self.save()
        # Mirror ensure()'s tail: replace the in-process cache entry so the
        # next request in this container gets the completed output instead
        # of revalidating (and re-running) against the incomplete one.
        from policyengine.core.simulation import _cache

        _cache.add(self.id, self)

    def _missing_output_columns(self) -> list[tuple[str, str]]:
        data = getattr(self.output_dataset, "data", None)
        frames = getattr(data, "entity_data", None)
        resolved = self.tax_benefit_model_version.resolve_entity_variables(self)
        if not frames:
            return [(entity, "<no output data>") for entity in resolved]
        missing: list[tuple[str, str]] = []
        for entity, variables in resolved.items():
            frame = frames.get(entity)
            columns = set(getattr(frame, "columns", ()))
            missing.extend(
                (entity, variable) for variable in variables if variable not in columns
            )
        return missing
