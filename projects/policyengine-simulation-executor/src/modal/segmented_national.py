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
in-band failure paths do.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import modal
from policyengine_observability import segment, set_attribute

from policyengine_simulation_executor.national_partition import (
    national_region_groups,
)
from policyengine_simulation_executor.segmented_national_reduce import (
    build_national_output,
)
from policyengine_simulation_observability.errors import log_and_redact_exception
from policyengine_simulation_observability.observability import SegmentName
from policyengine_simulation_observability.telemetry import split_internal_payload
from src.modal.fanout import build_child_payload, next_backoff

logger = logging.getLogger(__name__)

POLL_INTERVAL_INITIAL_SECONDS = 0.5
POLL_INTERVAL_MAX_SECONDS = 8.0
POLL_INTERVAL_BACKOFF_FACTOR = 2.0

# The dedicated children pool (see module docstring).
SEGMENT_FUNCTION_NAME = "run_simulation_segment"

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
    """The request SHAPE of a plain US national macro run (ignores the
    eligibility knobs — see ``should_run_segmented_national``).

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
    """True when a request should fan out across the national partition.

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


class SegmentedNationalRunner:
    """Runs one segmented national request to completion."""

    def __init__(
        self,
        params: dict[str, Any],
        *,
        app_name: str,
        modal_module=None,
        poll_interval_seconds: float = POLL_INTERVAL_INITIAL_SECONDS,
        poll_interval_max_seconds: float = POLL_INTERVAL_MAX_SECONDS,
    ):
        self.params = params
        self.app_name = app_name
        self.modal = modal if modal_module is None else modal_module
        self.poll_interval_initial_seconds = poll_interval_seconds
        self.poll_interval_max_seconds = poll_interval_max_seconds
        self.country = str(params.get("country") or "us").lower()
        groups = national_region_groups(self.country)
        if not groups:
            raise ValueError(f"No national partition for country {self.country!r}")
        self.groups = groups
        # Lazy handle, no RPC until first spawn (budget-window precedent).
        self.child_func = self.modal.Function.from_name(app_name, SEGMENT_FUNCTION_NAME)
        set_attribute("national_execution", "segmented")
        set_attribute("segmented_group_count", len(self.groups))
        set_attribute("country", self.country)
        set_attribute("region", self.country)
        if params.get("time_period"):
            set_attribute("simulation_year", str(params["time_period"]))

    def run(self) -> dict[str, Any]:
        country_module = self._country_module()
        self._extend_last_group_with_uncovered_states(country_module)
        handles = self._spawn_all_children()
        child_results = self._collect(handles)
        return self._reduce(child_results, country_module=country_module)

    def _country_module(self):
        from policyengine_simulation_executor.simulation_runtime import (
            _country_module,
        )

        return _country_module(self.country)

    def _extend_last_group_with_uncovered_states(self, country_module) -> None:
        """Registry state regions missing from the static partition ride in
        the last group, loudly, so a new state-level region added to the
        model cannot silently drop its households from national results.
        (Households whose state has NO registered region remain invisible
        here; the partition test pins the registry at pin-bump time.)
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
            set_attribute("partition_uncovered_regions", ",".join(uncovered))
            self.groups[-1] = list(self.groups[-1]) + uncovered

    def _spawn_all_children(self) -> list[tuple[list[str], Any]]:
        handles: list[tuple[list[str], Any]] = []
        try:
            for group in self.groups:
                payload = build_group_child_payload(self.params, group)
                with segment(
                    SegmentName.SEGMENTED_NATIONAL_CHILD_SPAWN,
                    region_group="+".join(group),
                ):
                    call = self.child_func.spawn(payload)
                handles.append((group, call))
        except Exception:
            # Children spawned before the failure must not run for a job
            # that is already dead.
            self._cancel_all(handles)
            raise
        return handles

    def _collect(self, handles: list[tuple[list[str], Any]]) -> list[dict]:
        results: list[dict | None] = [None] * len(handles)
        pending = set(range(len(handles)))
        poll_errors: dict[int, int] = {}
        current_sleep = self.poll_interval_initial_seconds
        poll_count = 0

        while pending:
            progress_made = False
            for index in sorted(pending):
                group, call = handles[index]
                poll_count += 1
                try:
                    results[index] = call.get(timeout=0)
                except TimeoutError:
                    # A not-ready probe is a SUCCESSFUL RPC: reset the error
                    # tally so only consecutive failures fail the job.
                    poll_errors.pop(index, None)
                    continue
                except Exception as exc:
                    # One transient poll-RPC blip must not kill the job; a
                    # real child failure re-raises on the next probe too.
                    attempts = poll_errors.get(index, 0) + 1
                    poll_errors[index] = attempts
                    if attempts < 2:
                        logger.warning(
                            "Poll error for group %s (attempt %d), retrying: %s",
                            "+".join(group),
                            attempts,
                            type(exc).__name__,
                        )
                        continue
                    redacted = log_and_redact_exception(
                        exc,
                        scope="segmented_national_child",
                        context={"region_group": "+".join(group)},
                    )
                    self._cancel_all(handles)
                    raise RuntimeError(
                        "Segmented national child failed for group "
                        f"{'+'.join(group)}: {redacted}"
                    ) from None
                pending.discard(index)
                poll_errors.pop(index, None)
                progress_made = True
            # Bounded aggregate instead of one segment per probe (see the
            # SegmentName NOTE about long poll loops).
            set_attribute("segmented_poll_count", poll_count)
            if pending and not progress_made:
                time.sleep(current_sleep)
                current_sleep = next_backoff(
                    current_sleep,
                    factor=POLL_INTERVAL_BACKOFF_FACTOR,
                    maximum=self.poll_interval_max_seconds,
                )
            elif progress_made:
                current_sleep = self.poll_interval_initial_seconds
        return results  # type: ignore[return-value]

    def _cancel_all(self, handles: list[tuple[list[str], Any]]) -> None:
        """Best-effort cancel so a failed batch doesn't strand workers."""
        for _, call in handles:
            try:
                call.cancel()
            except Exception:
                pass

    def _reduce(self, child_results: list[dict], *, country_module) -> dict[str, Any]:
        from policyengine_simulation_executor.simulation_runtime import (
            _parse_year,
            _requested_data_version,
        )

        # The canonical internal-key stripper, plus the opt-out knob.
        simulation_params, _, _ = split_internal_payload(self.params)
        simulation_params.pop("segmented", None)
        with segment(SegmentName.SEGMENTED_NATIONAL_REDUCE):
            output = build_national_output(
                child_results,
                country=self.country,
                simulation_params=simulation_params,
                country_module=country_module,
                year=_parse_year(simulation_params),
                resolved_data_version=_requested_data_version(simulation_params),
            )
        for key in ("model_version", "data_version"):
            if output.get(key):
                set_attribute(key, str(output[key]))
        return output


def run_segmented_national_impl(
    params: dict[str, Any], *, app_name: str
) -> dict[str, Any]:
    return SegmentedNationalRunner(params, app_name=app_name).run()


def dispatch_run_simulation(params: dict[str, Any], *, app_name: str) -> dict[str, Any]:
    """The run_simulation entrypoint's routing: segmented national fan-out
    for eligible requests, the monolithic path for everything else."""
    if should_run_segmented_national(params):
        return run_segmented_national_impl(params, app_name=app_name)

    from policyengine_simulation_executor.simulation_runtime import (
        run_simulation_impl,
    )

    # Only label runs that are actually national-shaped: children, regional,
    # and UK requests must not pollute the segmented-vs-monolithic metric.
    if is_plain_national_macro(params):
        set_attribute("national_execution", "monolithic")
    return run_simulation_impl(params)
