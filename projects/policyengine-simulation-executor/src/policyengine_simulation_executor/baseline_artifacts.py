"""Deterministic baseline simulation ids and the validate-on-load guard.

The runtime read path of the artifact pipeline. A current-law baseline whose
identity is fully determined by the installed version-set gets a
deterministic ``Simulation.id`` from ``artifact_keys``: its level handler
(``stage12_cache.levels``) decides which runs qualify. ``Simulation.ensure()``
then finds ``{storage id}.h5`` beside the year file when a precomputed
baseline was baked into the image's prebuilt folder, and loads in seconds
instead of running the model.

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

from policyengine_simulation_contract.spm import SPMInputError, spm_error_detail
from pydantic import PrivateAttr

from policyengine_simulation_executor import artifact_keys
from policyengine_simulation_executor.precompute_models import ArtifactOutcome
from policyengine_simulation_executor.region_resolution import RegionResolution
from policyengine_simulation_executor.stage12_cache import levels
from policyengine_simulation_executor.stage_reporting_simulation import (
    StageReportingSimulation,
)

if TYPE_CHECKING:
    from policyengine.core import Simulation
    from policyengine.core.scoping_strategy import ScopingStrategy

    from policyengine_simulation_executor.simulation_runtime import DatasetSelection

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

    ``region_code`` is the normalized region (None when unresolved),
    ``scoping_strategy`` narrows the simulation to it (None for a whole
    dataset, as a national run is), ``state_code`` is the US state a state or
    district region lies in, and ``dataset`` is the selected dataset.
    """

    params: dict[str, Any]
    country: str
    dataset: DatasetSelection
    policy: object
    region_code: str | None
    scoping_strategy: ScopingStrategy | None
    year: int
    state_code: str | None = None

    def run_scope(self) -> levels.RunScope | None:
        """Return the run the level handlers look at; None for an unresolved region."""
        if self.region_code is None:
            return None
        return levels.RunScope(
            country=self.country.lower(),
            year=self.year,
            region=RegionResolution(
                code=self.region_code,
                scoping_strategy=self.scoping_strategy,
                state_code=self.state_code,
            ),
            dataset=self.dataset,
        )


def qualifying_baseline_identity(
    request: BaselineIdentityRequest,
) -> artifact_keys.BaselineIdentity | None:
    """Full artifact identity for a qualifying baseline, else None.

    Qualifying means the simulation's bytes are a pure function of the
    installed version-set: current-law policy, macro scope, and a run whose
    level handler gives a baseline scope (today the unscoped US and UK
    national runs and the US groups of states). The handler decides by the
    run alone, never by the precomputed years: a qualifying run in a year that
    is not precomputed still gets a fixed ID, misses, and computes — a free
    in-process-cache win for warm containers.

    Both the runtime reader (via ``deterministic_baseline_id``) and the
    precompute writer derive identity through THIS function, so writer and
    reader cannot disagree by construction. Identity-collection errors
    propagate: the writer must fail loudly on them.
    """
    params = request.params
    if request.policy is not None or str(params.get("scope") or "").lower() != "macro":
        return None
    run = request.run_scope()
    prebuilt = None if run is None else levels.prebuilt_for(run.country)
    handler = None if run is None or prebuilt is None else prebuilt.handler_for_run(run)
    if run is None or handler is None:
        return None
    scope = handler.baseline_scope(run)
    year_file = None if scope is None else handler.year_file_for(run)
    if scope is None or year_file is None:
        return None
    return artifact_keys.baseline_identity(
        artifact_keys.year_file_identity(year_file), scope, params.get("spm")
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
