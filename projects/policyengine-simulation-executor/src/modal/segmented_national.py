"""Fan out a plain national macro request across region groups.

A US national macro request runs segmented BY DEFAULT: one ``region_group``
child per partition group (spawned into the dedicated
``run_simulation_segment`` worker pool), then the children's computed
microdata reduces into the national output via the existing builder
(``segmented_national_reduce``). The request and response shapes are
identical to the monolithic path — the gateway and its clients see nothing
but a faster job. ``segmented: false`` opts out.

Children get their own container pool because a blocking parent must never
share a bounded pool with the work it waits on: parents live in
``run_simulation`` (gateway-facing), children in ``run_simulation_segment``.

There is no batch state store: the plain job-poll path needs no partial
status, so a parent failure propagates to the gateway as a failed job. A
parent killed outright (its own timeout/OOM) cannot cancel children — the
in-band failure paths do. The parent therefore enforces its own deadlines
below the Modal timeout: a child with no result within its deadline is
cancelled and respawned once, and the run fails in-band (cancelling every
child) before the parent's Modal timeout would kill it.
"""

from __future__ import annotations

import contextlib
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from policyengine_simulation_contract.job_progress import JobExecution, JobStage
from policyengine_simulation_contract.spm import (
    SPMInputError,
    combine_spm_results,
    spm_error_detail,
)
from policyengine_simulation_executor.job_progress import (
    NULL_JOB_PROGRESS,
    JobProgressReporter,
)
from policyengine_simulation_executor.national_partition import (
    national_region_groups,
)
from policyengine_simulation_executor.segmented_national_reduce import (
    build_national_output,
)
from policyengine_simulation_observability.errors import log_and_redact_exception
from policyengine_simulation_observability.stages import (
    SEGMENTED_NATIONAL_STAGES,
    Stage,
)
from policyengine_simulation_observability.telemetry import split_internal_payload
from src.modal.fanout import build_child_payload, next_backoff

import modal

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import ModuleType

    from policyengine_observability import ObservabilityRuntime

logger = logging.getLogger(__name__)

POLL_INTERVAL_INITIAL_SECONDS = 0.5
POLL_INTERVAL_MAX_SECONDS = 8.0
POLL_INTERVAL_BACKOFF_FACTOR = 2.0

# The dedicated children pool (see module docstring).
SEGMENT_FUNCTION_NAME = "run_simulation_segment"

# Seconds from dispatch before a child with no result is cancelled and
# respawned. Successful children took 124 s at the median and at most 302 s
# in the week to 13 October 2026, apart from one 2,672 s outlier.
CHILD_DEADLINE_SECONDS = 1200.0
# Must equal the run_simulation timeout in src/modal/app.py.
PARENT_FUNCTION_TIMEOUT_SECONDS = 3600.0
# Parent time kept back for the reduce, result return, and the work done
# before this runner starts.
PARENT_COMPLETION_MARGIN_SECONDS = 300.0
# A respawn needs at least this long to be worth starting (the slowest
# successful children took about 300 s).
MINIMUM_RETRY_WINDOW_SECONDS = 300.0
# The first dispatch plus one respawn after a missed deadline.
MAX_CHILD_ATTEMPTS = 2

# Parent-scoped keys never forwarded to children. (The shared payload
# builder owns _telemetry: stripped, then re-attached when present.)
# `region` is stripped because v1-style national parents carry
# region:"us", and a child must scope by its region_group alone.
CHILD_STRIP_FIELDS = frozenset({"segmented", "_metadata", "region"})

# Reforms under this prefix activate labor-supply behavioral responses.
# policyengine decides LSR activity from simulation.policy — which the
# reduce's stand-in simulations do not carry — so an LSR reform through the
# reduce would silently report labor_supply_response as all zeros. Worse,
# the naive fix (attaching the policy to the stand-ins) mislabels the LSR
# decile splits: the transported household_income_decile column is computed
# per region group, not nationally. Until the reduce recomputes national
# deciles and is validated for LSR, these reforms run monolithic.
LSR_PARAMETER_PREFIX = "gov.simulation.labor_supply_responses"


def _activates_labor_supply_response(params: dict[str, Any]) -> bool:
    for policy_key in ("reform", "baseline"):
        policy = params.get(policy_key)
        if isinstance(policy, dict) and any(
            str(name).startswith(LSR_PARAMETER_PREFIX) for name in policy
        ):
            return True
    return False


def is_plain_national_macro(params: dict[str, Any]) -> bool:
    """Return whether ``params`` has the request SHAPE of a plain US national macro run.

    Ignores the eligibility knobs (see ``should_run_segmented_national``).
    National is spelled two ways in the wild: region omitted (the sim API's
    own convention) and ``region: "us"`` (what API v1 sends on every
    national run). Both must match, mirroring the worker's normalisation
    (``_normalise_region_code``: None/empty/"us" -> country).
    """
    country = str(params.get("country") or "us").lower()
    if country != "us":
        return False
    if str(params.get("scope") or "").lower() != "macro":
        return False
    if params.get("region_group"):
        return False
    region = str(params.get("region") or "").strip().lower()
    return region in ("", country)


def should_run_segmented_national(params: dict[str, Any]) -> bool:
    """Return True when a request should fan out across the national partition.

    Eligible: a plain US national macro shape, not opted out via
    ``segmented: false``. Falls back to monolithic for ``include_cliffs``
    (cliff-variable reconstruction through the reduce is not yet validated)
    and for labor-supply-response reforms (see ``LSR_PARAMETER_PREFIX``).
    """
    if not is_plain_national_macro(params):
        return False
    if params.get("segmented") is False:
        return False
    if params.get("include_cliffs"):
        return False
    if _activates_labor_supply_response(params):
        return False
    country = str(params.get("country") or "us").lower()
    return national_region_groups(country) is not None


def build_group_child_payload(
    params: dict[str, Any], group: list[str]
) -> dict[str, Any]:
    """One child's request: the parent's params scoped to a region group."""
    return build_child_payload(
        params,
        strip_fields=CHILD_STRIP_FIELDS,
        overrides={"region_group": list(group), "_emit_microdata": True},
    )


@dataclass
class ChildExecution:
    """One dispatched attempt at a region group's child simulation."""

    group: list[str]
    call: Any
    attempt: int
    dispatched_at: float
    deadline_seconds: float

    @property
    def group_label(self) -> str:
        """Region codes joined with "+", as logs and telemetry name the group."""
        return "+".join(self.group)


class SegmentedNationalRunner:
    """Runs one segmented national request to completion."""

    def __init__(
        self,
        params: dict[str, Any],
        *,
        app_name: str,
        runtime: ObservabilityRuntime,
        modal_module: ModuleType | None = None,
        poll_interval_seconds: float = POLL_INTERVAL_INITIAL_SECONDS,
        poll_interval_max_seconds: float = POLL_INTERVAL_MAX_SECONDS,
        child_deadline_seconds: float = CHILD_DEADLINE_SECONDS,
        parent_budget_seconds: float = (
            PARENT_FUNCTION_TIMEOUT_SECONDS - PARENT_COMPLETION_MARGIN_SECONDS
        ),
        clock: Callable[[], float] = time.monotonic,
        progress: JobProgressReporter = NULL_JOB_PROGRESS,
    ) -> None:
        """Resolve the country's region groups and a lazy segment-worker handle.

        Raises ValueError when the country has no national partition.
        """
        self.params = params
        self.app_name = app_name
        self.runtime = runtime
        self.progress = progress
        self.modal = modal if modal_module is None else modal_module
        self.poll_interval_initial_seconds = poll_interval_seconds
        self.poll_interval_max_seconds = poll_interval_max_seconds
        self.child_deadline_seconds = child_deadline_seconds
        self.parent_budget_seconds = parent_budget_seconds
        self.clock = clock
        # The parent's Modal timeout started before this runner existed; the
        # completion margin inside the default budget covers that gap.
        self.started_at = clock()
        self.budget_ends_at = self.started_at + parent_budget_seconds
        self.child_retry_count = 0
        # Overdue attempts replaced by a respawn. They are never polled
        # again, but every later cancel retries them: the first cancel can
        # fail, and Modal keeps re-running a stuck input until one succeeds.
        self.superseded_children: list[ChildExecution] = []
        self.country = str(params.get("country") or "us").lower()
        groups = national_region_groups(self.country)
        if not groups:
            msg = f"No national partition for country {self.country!r}"
            raise ValueError(msg)
        self.groups = groups
        # Lazy handle, no RPC until first spawn (budget-window precedent).
        self.child_func = self.modal.Function.from_name(app_name, SEGMENT_FUNCTION_NAME)
        self.runtime.set_context(
            national_execution="segmented",
            segmented_group_count=len(self.groups),
            country=self.country,
            region=self.country,
        )
        if params.get("time_period"):
            self.runtime.set_context(simulation_year=str(params["time_period"]))

    def run(self) -> dict[str, Any]:
        """Spawn a child per region group, wait for all, and reduce to national output.

        Any child failure cancels the remaining children and raises.
        """
        country_module = self._country_module()
        self._extend_last_group_with_uncovered_states(country_module)
        self.progress.start_segments(len(self.groups))
        children = self._spawn_all_children()
        child_results = self._collect(children)
        self.progress.enter(JobStage.OUTPUTS)
        return self._reduce(child_results, country_module=country_module)

    def _country_module(self) -> ModuleType:
        from policyengine_simulation_executor.simulation_runtime import (  # noqa: PLC0415  # resolved per call so tests can patch simulation_runtime._country_module
            _country_module,
        )

        return _country_module(self.country)

    def _extend_last_group_with_uncovered_states(
        self, country_module: ModuleType
    ) -> None:
        """Append registry state regions missing from the partition to the last group.

        They ride along loudly (a warning and a run attribute) so a new
        state-level region added to the model cannot silently drop its
        households from national results. (Households whose state has NO
        registered region remain invisible here; the partition test pins the
        registry at pin-bump time.)
        """
        # region_registry is populated eagerly in the model version's
        # __init__; None only occurs for stub models (unit tests).
        registry = getattr(country_module.model, "region_registry", None)
        if registry is None:
            return
        state_codes = {
            region.code for region in registry.regions if region.region_type == "state"
        }
        covered = {code for group in self.groups for code in group}
        uncovered = sorted(state_codes - covered)
        if uncovered:
            logger.warning(
                "National partition is missing registry state regions %s; "
                "assigning them to the last group for this run. Rebalance "
                "US_NATIONAL_REGION_GROUPS with a measured partition.",
                uncovered,
            )
            self.runtime.set_context(partition_uncovered_regions=",".join(uncovered))
            self.groups[-1] = list(self.groups[-1]) + uncovered

    def _spawn_child(self, group: list[str], *, attempt: int) -> modal.FunctionCall:
        payload = build_group_child_payload(self.params, group)
        observability_context = self.runtime.capture_context()
        with self.runtime.span(
            SEGMENTED_NATIONAL_STAGES.name(Stage.SEGMENTED_NATIONAL_CHILD_SPAWN),
            attributes={"region_group": "+".join(group), "child_attempt": attempt},
        ):
            return self.child_func.spawn(
                payload,
                observability_context=observability_context,
            )

    def _spawn_all_children(self) -> list[ChildExecution]:
        children: list[ChildExecution] = []
        try:
            for group in self.groups:
                call = self._spawn_child(group, attempt=1)
                children.append(
                    ChildExecution(
                        group=group,
                        call=call,
                        attempt=1,
                        dispatched_at=self.clock(),
                        deadline_seconds=self.child_deadline_seconds,
                    )
                )
        except Exception:
            # Children spawned before the failure must not run for a job
            # that is already dead.
            self._cancel_all(children)
            raise
        return children

    def _collect(self, children: list[ChildExecution]) -> list[dict]:
        results: list[dict | None] = [None] * len(children)
        pending = set(range(len(children)))
        poll_errors: dict[int, int] = {}
        current_sleep = self.poll_interval_initial_seconds
        poll_count = 0

        while pending:
            progress_made = False
            for index in sorted(pending):
                child = children[index]
                poll_count += 1
                try:
                    results[index] = child.call.get(timeout=0)
                except TimeoutError:
                    # A not-ready probe is a SUCCESSFUL RPC: reset the error
                    # tally so only consecutive failures fail the job.
                    poll_errors.pop(index, None)
                    now = self.clock()
                    if now - child.dispatched_at >= child.deadline_seconds:
                        children[index] = self._respawn_overdue_child(
                            children, index, now=now
                        )
                    continue
                except Exception as exc:
                    detail = spm_error_detail(exc)
                    if detail:
                        self._cancel_all(children)
                        raise SPMInputError(detail.code, detail.message) from exc
                    # One transient poll-RPC blip must not kill the job; a
                    # real child failure re-raises on the next probe too.
                    attempts = poll_errors.get(index, 0) + 1
                    poll_errors[index] = attempts
                    if attempts < 2:
                        logger.warning(
                            "Poll error for group %s (attempt %d), retrying: %s",
                            child.group_label,
                            attempts,
                            type(exc).__name__,
                        )
                        continue
                    redacted = log_and_redact_exception(
                        exc,
                        runtime=self.runtime,
                        scope="segmented_national_child",
                        context={
                            "region_group": child.group_label,
                            "child_attempt": child.attempt,
                        },
                    )
                    self._cancel_all(children)
                    retry_note = (
                        f" on attempt {child.attempt}, after a deadline respawn"
                        if child.attempt > 1
                        else ""
                    )
                    msg = (
                        "Segmented national child failed for group "
                        f"{child.group_label}{retry_note}: {redacted}"
                    )
                    raise RuntimeError(msg) from None
                pending.discard(index)
                poll_errors.pop(index, None)
                progress_made = True
                self.progress.segment_completed()
            # Bounded aggregate instead of one segment per probe (see the
            # Stage registry note about long poll loops).
            self.runtime.set_context(segmented_poll_count=poll_count)
            self._fail_if_budget_exhausted(children, pending)
            if pending and not progress_made:
                time.sleep(current_sleep)
                current_sleep = next_backoff(
                    current_sleep,
                    factor=POLL_INTERVAL_BACKOFF_FACTOR,
                    maximum=self.poll_interval_max_seconds,
                )
            elif progress_made:
                current_sleep = self.poll_interval_initial_seconds
        # Every group has a result; stop any superseded attempt whose first
        # cancel failed.
        self._cancel_superseded_children()
        return results  # type: ignore[return-value]

    def _respawn_overdue_child(
        self, children: list[ChildExecution], index: int, *, now: float
    ) -> ChildExecution:
        """Replace a child that missed its deadline, or fail the run.

        Modal can keep re-running a child whose containers are terminated, so
        a stuck child never fails on its own; only the parent can stop it.
        """
        overdue = children[index]
        waited_seconds = now - overdue.dispatched_at
        remaining_budget_seconds = self.budget_ends_at - now
        if overdue.attempt >= MAX_CHILD_ATTEMPTS:
            reason = (
                f"no result {waited_seconds:.0f}s after dispatch on attempt "
                f"{overdue.attempt} of {MAX_CHILD_ATTEMPTS}"
            )
        elif remaining_budget_seconds < MINIMUM_RETRY_WINDOW_SECONDS:
            reason = (
                f"no result {waited_seconds:.0f}s after dispatch, and only "
                f"{max(remaining_budget_seconds, 0):.0f}s of the parent budget "
                "remains for a retry"
            )
        else:
            reason = None
        if reason is not None:
            self.runtime.event(
                "segmented_national_child_timed_out",
                severity="ERROR",
                attributes={
                    "region_group": overdue.group_label,
                    "child_attempt": overdue.attempt,
                    "waited_seconds": round(waited_seconds, 3),
                    "deadline_seconds": overdue.deadline_seconds,
                    "remaining_budget_seconds": round(
                        max(remaining_budget_seconds, 0), 3
                    ),
                },
            )
            self._cancel_all(children)
            msg = (
                "Segmented national child timed out for group "
                f"{overdue.group_label}: {reason}"
            )
            raise RuntimeError(msg) from None

        self._cancel_call(overdue.call)
        self.superseded_children.append(overdue)
        next_attempt = overdue.attempt + 1
        try:
            call = self._spawn_child(overdue.group, attempt=next_attempt)
        except Exception:
            self._cancel_all(children)
            raise
        self.child_retry_count += 1
        logger.warning(
            "Group %s had no result %.0fs after dispatch; respawned it "
            "(attempt %d of %d)",
            overdue.group_label,
            waited_seconds,
            next_attempt,
            MAX_CHILD_ATTEMPTS,
        )
        self.runtime.event(
            "segmented_national_child_respawned",
            severity="WARNING",
            attributes={
                "region_group": overdue.group_label,
                "child_attempt": next_attempt,
                "waited_seconds": round(waited_seconds, 3),
                "deadline_seconds": overdue.deadline_seconds,
            },
        )
        self.runtime.set_context(segmented_child_retry_count=self.child_retry_count)
        return ChildExecution(
            group=overdue.group,
            call=call,
            attempt=next_attempt,
            dispatched_at=self.clock(),
            deadline_seconds=min(self.child_deadline_seconds, remaining_budget_seconds),
        )

    def _fail_if_budget_exhausted(
        self, children: list[ChildExecution], pending: set[int]
    ) -> None:
        """Fail in-band, cancelling children, once the parent budget is spent.

        Failing here, before the Modal timeout can kill the parent, keeps the
        children from being stranded.
        """
        if not pending:
            return
        now = self.clock()
        if now < self.budget_ends_at:
            return
        outstanding = [children[index] for index in sorted(pending)]
        outstanding_groups = ", ".join(child.group_label for child in outstanding)
        self.runtime.event(
            "segmented_national_budget_exhausted",
            severity="ERROR",
            attributes={
                "region_groups": outstanding_groups,
                "child_attempts": ", ".join(
                    str(child.attempt) for child in outstanding
                ),
                "waited_seconds": round(now - self.started_at, 3),
                "parent_budget_seconds": self.parent_budget_seconds,
            },
        )
        self._cancel_all(children)
        msg = (
            "Segmented national run exceeded its "
            f"{self.parent_budget_seconds:.0f}s budget with groups still "
            f"running: {outstanding_groups}"
        )
        raise RuntimeError(msg)

    @staticmethod
    def _cancel_call(call: modal.FunctionCall) -> None:
        # Best-effort cleanup: a failed cancel must not mask the failure being
        # raised, and superseded calls are cancelled again later.
        with contextlib.suppress(Exception):
            call.cancel()

    def _cancel_superseded_children(self) -> None:
        for superseded in self.superseded_children:
            self._cancel_call(superseded.call)

    def _cancel_all(self, children: list[ChildExecution]) -> None:
        """Best-effort cancel so a failed batch doesn't strand workers."""
        for child in children:
            self._cancel_call(child.call)
        self._cancel_superseded_children()

    def _reduce(
        self, child_results: list[dict], *, country_module: ModuleType
    ) -> dict[str, Any]:
        from policyengine_simulation_executor.simulation_runtime import (  # noqa: PLC0415  # Modal worker code: bound per call, like this module's other simulation_runtime imports
            _parse_year,
        )

        # The canonical internal-key stripper, plus the opt-out knob.
        simulation_params, _, _ = split_internal_payload(self.params)
        simulation_params.pop("segmented", None)
        with self.runtime.span(
            SEGMENTED_NATIONAL_STAGES.name(Stage.SEGMENTED_NATIONAL_REDUCE)
        ):
            output = build_national_output(
                child_results,
                country=self.country,
                simulation_params=simulation_params,
                country_module=country_module,
                year=_parse_year(simulation_params),
                resolved_data_version=None,
                runtime=self.runtime,
            )
        output.update(
            combine_spm_results(
                child_results,
                simulation_params.get("spm"),
                expected_year=_parse_year(simulation_params),
            )
        )
        for key in ("model_version", "data_version"):
            if output.get(key):
                self.runtime.set_context(**{key: str(output[key])})
        return output


def run_segmented_national_impl(
    params: dict[str, Any],
    *,
    app_name: str,
    runtime: ObservabilityRuntime,
    progress: JobProgressReporter = NULL_JOB_PROGRESS,
) -> dict[str, Any]:
    """Run ``params`` as a segmented national fan-out on ``app_name``'s segment pool."""
    return SegmentedNationalRunner(
        params,
        app_name=app_name,
        runtime=runtime,
        progress=progress,
    ).run()


def dispatch_run_simulation(
    params: dict[str, Any],
    *,
    app_name: str,
    runtime: ObservabilityRuntime,
    progress: JobProgressReporter = NULL_JOB_PROGRESS,
) -> dict[str, Any]:
    """Route a ``run_simulation`` request to segmented fan-out or the monolithic path.

    Segmented national fan-out serves eligible requests (see
    ``should_run_segmented_national``); everything else runs monolithically.
    """
    from policyengine_simulation_executor.spm import (
        normalize_runtime_spm,
    )

    selection = normalize_runtime_spm(params)
    if selection is not None:
        params = {**params, "spm": selection}
    if should_run_segmented_national(params):
        progress.set_execution(JobExecution.SEGMENTED)
        return run_segmented_national_impl(
            params,
            app_name=app_name,
            runtime=runtime,
            progress=progress,
        )

    from policyengine_simulation_executor.simulation_runtime import (  # noqa: PLC0415  # resolved per call so tests can patch simulation_runtime.run_simulation_impl
        run_simulation_impl,
    )

    # Only label runs that are actually national-shaped: children, regional,
    # and UK requests must not pollute the segmented-vs-monolithic metric.
    if is_plain_national_macro(params):
        runtime.set_context(national_execution="monolithic")
    progress.set_execution(JobExecution.MONOLITHIC)
    return run_simulation_impl(params, runtime=runtime, progress=progress)
