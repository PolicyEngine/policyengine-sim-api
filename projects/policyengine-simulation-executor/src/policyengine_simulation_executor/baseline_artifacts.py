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
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Protocol, runtime_checkable

from policyengine.core.scoping_strategy import RegionGroupStrategy
from policyengine_simulation_contract.spm import SPMInputError, spm_error_detail
from pydantic import PrivateAttr

from policyengine_simulation_executor import artifact_keys
from policyengine_simulation_executor.precompute_models import ArtifactOutcome
from policyengine_simulation_executor.stage_reporting_simulation import (
    StageReportingSimulation,
)

if TYPE_CHECKING:
    from policyengine.core import Simulation
    from policyengine.core.scoping_strategy import ScopingStrategy

logger = logging.getLogger(__name__)

# Artifact outcomes reported via the `baseline_artifact` observability
# attribute. "hit": loaded (disk or in-process cache) and complete;
# "incomplete": loaded but missing requested columns or a valid SPM receipt
# -> recomputed;
# "miss": no artifact -> computed (today's behavior).
OUTCOME_HIT: Final = "hit"
OUTCOME_INCOMPLETE: Final = "incomplete"
OUTCOME_MISS: Final = "miss"


@runtime_checkable
class EntityVariableResolver(Protocol):
    """A model version that lists the output variables a simulation must carry.

    policyengine defines ``resolve_entity_variables`` on
    MicrosimulationModelVersion, not on the TaxBenefitModelVersion a
    Simulation declares.
    """

    def resolve_entity_variables(self, simulation: Simulation) -> dict[str, list[str]]:
        """Return each entity's required output variables."""
        ...


@dataclass(frozen=True)
class BaselineIdentityRequest:
    """The request facts that decide whether a baseline has a deterministic id.

    ``region_code`` is the normalized region (None when unresolved) and
    ``scoping_strategy`` narrows the simulation to it (None for a whole
    dataset, as a national run is).
    """

    params: dict[str, Any]
    country: str
    dataset_is_default: bool
    policy: object
    region_code: str | None
    scoping_strategy: ScopingStrategy | None
    year: int


def _artifact_scope(
    request: BaselineIdentityRequest,
) -> tuple[str, str | None] | None:
    """Return the artifact's region label and scope key, or None if unsupported.

    Supported scopes are an unscoped national run and a region group.
    """
    if request.region_code is None:
        return None
    if request.scoping_strategy is None:
        if request.region_code != request.country.lower():
            return None
        return artifact_keys.NATIONAL_REGION, None
    if isinstance(request.scoping_strategy, RegionGroupStrategy):
        return request.region_code, request.scoping_strategy.cache_key
    return None


def qualifying_baseline_identity(
    request: BaselineIdentityRequest,
) -> artifact_keys.BaselineArtifactIdentity | None:
    """Full artifact identity for a qualifying baseline, else None.

    Qualifying means the simulation's bytes are a pure function of the
    installed version-set: current-law policy, macro scope, the resolved
    default dataset, and a scope the pipeline produces
    artifacts for — unscoped national or a region group. Single states stay
    on random ids in v1.

    Deliberately broader than what the store contains: UK national and
    off-precompute years qualify too, get deterministic ids, miss, and
    compute — a free in-process-cache win for warm containers, and their
    artifacts appear automatically if the precompute ever produces them.

    Both the runtime reader (via ``deterministic_baseline_id``) and the
    precompute writer derive identity through THIS function, so writer and
    reader cannot disagree by construction. Identity-collection errors
    propagate: the writer must fail loudly on them.
    """
    params = request.params
    if request.policy is not None:
        return None
    if str(params.get("scope") or "").lower() != "macro":
        return None
    if not request.dataset_is_default:
        return None
    scope = _artifact_scope(request)
    if scope is None:
        return None
    region, scope_key = scope

    return artifact_keys.collect_baseline_identity(
        request.country.lower(),
        request.year,
        region=region,
        scope_key=scope_key,
        **({"spm": params["spm"]} if params.get("spm") is not None else {}),
    )


def deterministic_baseline_id(request: BaselineIdentityRequest) -> str | None:
    """Return the deterministic simulation id for a qualifying baseline, else None.

    This is the runtime reader's wrapper around ``qualifying_baseline_identity``.
    Unlike the writer, the read path must never fail a request over
    identity collection (a manifest/receipt hiccup) — it degrades to a
    random id, meaning no artifact reuse for that request. SPM selection
    errors still propagate, since they mean the request itself is invalid.
    """
    try:
        identity = qualifying_baseline_identity(request)
    except Exception as exc:
        if spm_error_detail(exc):
            raise
        logger.warning(
            "Could not collect baseline artifact identity for %s; "
            "using a random simulation id",
            request.country,
            exc_info=True,
        )
        return None
    return None if identity is None else identity.simulation_id


class ArtifactBaselineSimulation(StageReportingSimulation):
    """A Simulation whose ``ensure()`` validates what it loads."""

    _computed_this_process: bool = PrivateAttr(default=False)
    _artifact_outcome: ArtifactOutcome | None = PrivateAttr(default=None)

    @property
    def artifact_outcome(self) -> ArtifactOutcome | None:
        """Set by ``ensure()``; None until then."""
        return self._artifact_outcome

    def run(self) -> None:
        """Run the model, noting that this process computed the output itself."""
        self._computed_this_process = True
        super().run()

    def _record_outcome(self, outcome: ArtifactOutcome) -> None:
        """First outcome wins for the life of this instance.

        Every request ensures its baseline more than once (the economic
        impact analysis and the decile impacts each call ``ensure()``), and
        after a miss the later calls self-hit the in-process cache with a
        now-complete column set — without the latch they would overwrite a
        genuine ``miss`` with ``hit`` and blind the rollout metric. The
        runtime builds a fresh instance per request, so latching per
        instance is latching per request.
        """
        if self._artifact_outcome is None:
            self._artifact_outcome = outcome

    def ensure(self) -> None:
        """Load or compute the output, recomputing when a loaded artifact is incomplete.

        A load counts only if it has every requested output column and, under
        an SPM selection, a valid receipt for the dataset year; otherwise the
        model runs, the result is saved and the in-process cache entry is
        replaced. Records the hit/miss/incomplete outcome on first call.
        """
        # The wrapper restores selection metadata on load/cache hits. Keep the
        # request's selection so an invalid cached entry cannot replace it.
        selection = getattr(self, "spm_config", None)
        requested_spm = getattr(self, "spm", None)
        self._computed_this_process = False
        super().ensure()
        if self._computed_this_process:
            self._record_outcome(OUTCOME_MISS)
            return

        from policyengine_simulation_executor.spm import (
            simulation_spm_result,
        )

        # A tax-only artifact can legitimately have no receipt years. Check its
        # columns first, then treat unusable cached receipts as another gap.
        missing = self._missing_output_columns()
        receipt_error = None
        if not missing and selection is not None:
            try:
                simulation_spm_result(
                    self, self, selection, expected_year=self.dataset.year
                )
            except SPMInputError as exc:
                receipt_error = str(exc)
        if not missing and receipt_error is None:
            self._record_outcome(OUTCOME_HIT)
            return

        logger.warning(
            "Baseline artifact %s is incomplete (missing columns=%s, "
            "SPM receipt=%s); recomputing",
            self.id,
            missing,
            receipt_error,
        )
        self._record_outcome(OUTCOME_INCOMPLETE)
        if selection is not None:
            self.spm = requested_spm
        self.run()
        self.save()
        # Replace the in-process cache entry so this request's second
        # ensure() and every later request in this container get the
        # completed output instead of revalidating (and re-running) against
        # the incomplete one. LRUCache.add keeps the OLD value for an
        # existing key and exposes no remove, so evict directly first.
        from policyengine.core.simulation import (  # noqa: PLC0415  # resolved per call so tests can patch policyengine's simulation cache
            _cache,
        )

        _cache._cache.pop(getattr(self, "storage_id", self.id), None)  # noqa: SLF001  # policyengine's LRUCache has no public remove
        _cache.add(
            getattr(self, "storage_id", self.id),
            self.model_copy(deep=False) if selection is not None else self,
        )

    def _missing_output_columns(self) -> list[tuple[str, str]]:
        data = getattr(self.output_dataset, "data", None)
        frames = getattr(data, "entity_data", None)
        model_version = self.tax_benefit_model_version
        if not isinstance(model_version, EntityVariableResolver):
            msg = "the baseline's model version can't list its output variables"
            raise TypeError(msg)
        resolved = model_version.resolve_entity_variables(self)
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
