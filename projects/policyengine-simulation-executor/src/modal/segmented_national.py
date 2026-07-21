"""Fan out a plain national macro request across region groups.

A US national macro request runs segmented BY DEFAULT: one ``region_group``
child per partition group (spawned back into this app's own ``run_simulation``
worker), then the children's computed microdata reduces into the national
output via the existing builder (``segmented_national_reduce``). The request
and response shapes are identical to the monolithic path — the gateway and
its clients see nothing but a faster job. ``segmented: false`` opts out.

Mirrors ``BudgetWindowBatchRunner``'s spawn/poll mechanics, minus the state
store: the plain job-poll path needs no partial status, so a parent failure
simply propagates to the gateway as a failed job, exactly like a monolithic
failure today.
"""

from __future__ import annotations

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

POLL_INTERVAL_INITIAL_SECONDS = 0.5
POLL_INTERVAL_MAX_SECONDS = 15.0
POLL_INTERVAL_BACKOFF_FACTOR = 2.0

# Never forwarded to children: the opt-out knob (children are region_group
# requests and thus ineligible regardless) and parent-scoped metadata.
SEGMENTED_STRIP_FIELDS = frozenset({"segmented", "_metadata", "_telemetry"})


def should_run_segmented_national(params: dict[str, Any]) -> bool:
    """True when a request should fan out across the national partition.

    Eligible: US, macro scope, no region/region_group, not opted out via
    ``segmented: false``. ``include_cliffs`` falls back to monolithic —
    cliff-variable reconstruction through the reduce is not yet validated.
    """
    country = str(params.get("country") or "us").lower()
    if country != "us":
        return False
    if str(params.get("scope") or "").lower() != "macro":
        return False
    if params.get("region") or params.get("region_group"):
        return False
    if params.get("segmented") is False:
        return False
    if params.get("include_cliffs"):
        return False
    return national_region_groups(country) is not None


def build_group_child_payload(
    params: dict[str, Any], group: list[str]
) -> dict[str, Any]:
    """One child's request: the parent's params scoped to a region group.

    Mirrors ``build_child_simulation_request`` (budget-window): copy the
    parent params minus the strip set, overwrite the fan-out axis, re-attach
    telemetry so child spans join the parent's trace.
    """
    payload = {
        key: value
        for key, value in params.items()
        if key not in SEGMENTED_STRIP_FIELDS
    }
    payload["region_group"] = list(group)
    payload["_emit_microdata"] = True
    telemetry = params.get("_telemetry")
    if isinstance(telemetry, dict):
        payload["_telemetry"] = telemetry
    return payload


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
        poll_interval_backoff_factor: float = POLL_INTERVAL_BACKOFF_FACTOR,
    ):
        self.params = params
        self.app_name = app_name
        self.modal = modal if modal_module is None else modal_module
        self.poll_interval_initial_seconds = poll_interval_seconds
        self.poll_interval_max_seconds = poll_interval_max_seconds
        self.poll_interval_backoff_factor = poll_interval_backoff_factor
        self.country = str(params.get("country") or "us").lower()
        groups = national_region_groups(self.country)
        if not groups:
            raise ValueError(
                f"No national partition for country {self.country!r}"
            )
        self.groups = groups
        # Lazy handle, no RPC until first spawn (budget-window precedent).
        self.child_func = self.modal.Function.from_name(
            app_name, "run_simulation"
        )
        set_attribute("national_execution", "segmented")
        set_attribute("segmented_group_count", len(self.groups))
        self._poll_count = 0

    def run(self) -> dict[str, Any]:
        handles = self._spawn_all_children()
        child_results = self._collect(handles)
        return self._reduce(child_results)

    def _spawn_all_children(self) -> list[tuple[list[str], Any]]:
        handles = []
        for group in self.groups:
            payload = build_group_child_payload(self.params, group)
            with segment(
                SegmentName.SEGMENTED_NATIONAL_CHILD_SPAWN,
                region_group="+".join(group),
            ):
                call = self.child_func.spawn(payload)
            handles.append((group, call))
        return handles

    def _collect(self, handles: list[tuple[list[str], Any]]) -> list[dict]:
        results: list[dict | None] = [None] * len(handles)
        pending = set(range(len(handles)))
        current_sleep = self.poll_interval_initial_seconds

        while pending:
            progress_made = False
            for index in sorted(pending):
                group, call = handles[index]
                self._poll_count += 1
                try:
                    results[index] = call.get(timeout=0)
                except TimeoutError:
                    continue
                except Exception as exc:
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
                progress_made = True
            # Bounded aggregate instead of one segment per probe (see the
            # SegmentName NOTE about long poll loops).
            set_attribute("segmented_poll_count", self._poll_count)
            if pending and not progress_made:
                time.sleep(current_sleep)
                current_sleep = min(
                    current_sleep * self.poll_interval_backoff_factor,
                    self.poll_interval_max_seconds,
                )
            elif progress_made:
                current_sleep = self.poll_interval_initial_seconds
        return results  # type: ignore[return-value]

    def _cancel_all(self, handles: list[tuple[list[str], Any]]) -> None:
        """Best-effort cancel so a failed batch doesn't strand 19 workers."""
        for _, call in handles:
            try:
                call.cancel()
            except Exception:
                pass

    def _reduce(self, child_results: list[dict]) -> dict[str, Any]:
        from policyengine_simulation_executor.simulation_runtime import (
            _country_module,
            _parse_year,
            _requested_data_version,
        )

        simulation_params = {
            key: value
            for key, value in self.params.items()
            if key not in SEGMENTED_STRIP_FIELDS
        }
        with segment(SegmentName.SEGMENTED_NATIONAL_REDUCE):
            return build_national_output(
                child_results,
                country=self.country,
                simulation_params=simulation_params,
                country_module=_country_module(self.country),
                year=_parse_year(simulation_params),
                resolved_data_version=_requested_data_version(
                    simulation_params
                ),
            )


def run_segmented_national_impl(
    params: dict[str, Any], *, app_name: str, modal_module=None
) -> dict[str, Any]:
    runner = SegmentedNationalRunner(
        params, app_name=app_name, modal_module=modal_module
    )
    return runner.run()


def dispatch_run_simulation(
    params: dict[str, Any], *, app_name: str
) -> dict[str, Any]:
    """The run_simulation entrypoint's routing: segmented national fan-out
    for eligible requests, the monolithic path for everything else."""
    if should_run_segmented_national(params):
        return run_segmented_national_impl(params, app_name=app_name)

    from policyengine_simulation_executor.simulation_runtime import (
        run_simulation_impl,
    )

    set_attribute("national_execution", "monolithic")
    return run_simulation_impl(params)
