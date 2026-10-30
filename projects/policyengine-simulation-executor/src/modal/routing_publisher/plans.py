"""Routing plans, their digests and the publication record that journals them."""

import hashlib
import json
import uuid
from collections.abc import Iterable
from copy import deepcopy
from typing import TYPE_CHECKING

from src.modal.routing_publisher.state import (
    ROUTING_PUBLICATION_KEY,
    DeployedAppVersions,
    build_next_routing_state,
)

if TYPE_CHECKING:
    import modal

# --- Routing plans: publish exactly once, restore exactly what was replaced ---
#
# A deployment writes its plan (the routing state before and after publishing)
# before it changes anything, keeps it as a workflow artifact, then applies
# it. A failed release is undone by restoring the plan's "before" state, but
# only while the live state is still the plan's "after" state: a restore never
# overwrites a change made by anyone else, and a plan that was never applied
# restores nothing.

ROUTING_PLAN_SCHEMA = "policyengine-simulation.routing-plan/v1"


class RoutingConflictError(RuntimeError):
    """The live routing state is not the one a plan expects."""


def routing_state_digest(state: object) -> str:
    """SHA-256 of a routing state's canonical JSON (``null`` when absent)."""
    text = json.dumps(state, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _json_copy[JsonValue](value: JsonValue) -> JsonValue:
    """Return a deep copy of JSON-safe ``value``; the round trip keeps its shape."""
    return json.loads(json.dumps(value))


def plan_routing_publication(
    *,
    current_state: object,
    deployed: DeployedAppVersions,
    force_latest: bool = False,
    publication_id: str | None = None,
) -> dict:
    """Plan the routing state before and after publishing a release, with digests.

    The published state's ``generation`` names this publication uniquely
    (``publication_id``, by default a random one), so a later publication of
    the same release is a different state and an old plan can never be
    restored over it. ``advances_latest`` says whether unpinned requests
    route to this release once the plan is applied.
    """
    publication_id = publication_id or uuid.uuid4().hex
    app_name = deployed.app_name
    policyengine_version = deployed.policyengine_version
    before = _json_copy(current_state) if current_state is not None else None
    after = _json_copy(
        build_next_routing_state(
            current_state=deepcopy(current_state),
            deployed=deployed,
            force_latest=force_latest,
        )
    )
    after["generation"] = f"{policyengine_version}:{app_name}:{publication_id}"
    return {
        "schema": ROUTING_PLAN_SCHEMA,
        "publication_id": publication_id,
        "policyengine_version": policyengine_version,
        "app_name": app_name,
        "before": before,
        "after": after,
        "before_sha256": routing_state_digest(before),
        "after_sha256": routing_state_digest(after),
        "advances_latest": after["latest"].get("policyengine") == policyengine_version,
    }


def validate_routing_plan(plan: object) -> dict:
    """Return a plan whose digests match its states, or raise ``ValueError``."""
    if not isinstance(plan, dict) or plan.get("schema") != ROUTING_PLAN_SCHEMA:
        msg = f"not a {ROUTING_PLAN_SCHEMA} routing plan"
        raise ValueError(msg)
    for side in ("before", "after"):
        if routing_state_digest(plan.get(side)) != plan.get(f"{side}_sha256"):
            msg = f"the plan's {side} state does not match its digest"
            raise ValueError(msg)
    if not isinstance(plan["after"], dict):
        msg = "the plan publishes no routing state"
        raise ValueError(msg)  # noqa: TRY004
    return plan


def unfinished_publication(store: "modal.Dict | None") -> dict | None:
    """Return the pending publication's plan, or None; refuse an invalid record."""
    record = store.get(ROUTING_PUBLICATION_KEY) if store is not None else None
    if record is None:
        return None
    if not isinstance(record, dict) or record.get("status") not in {
        "pending",
        "completed",
        "restored",
    }:
        msg = "invalid routing publication record; refusing routing writes"
        raise RoutingConflictError(msg)
    validate_routing_plan(record.get("plan"))
    return record["plan"] if record["status"] == "pending" else None


def require_finished_publication(
    store: "modal.Dict | None", *, allowed_plans: Iterable[dict] = ()
) -> None:
    """Refuse routing writes while a publication not in ``allowed_plans`` is pending."""
    pending = unfinished_publication(store)
    if pending is not None and not any(pending == plan for plan in allowed_plans):
        msg = (
            f"Unfinished routing publication {pending['publication_id']}: "
            "refusing new routing writes. Inspect --print-pending-plan and restore "
            "the recorded plan through simulation-rollback with target and "
            "resolve_pending_publication=true before deploying again."
        )
        raise RoutingConflictError(msg)


def write_publication_record(store: "modal.Dict", *, plan: dict, status: str) -> None:
    """Write the publication record and check that it reads back the same."""
    record = {"status": status, "plan": _json_copy(plan)}
    store[ROUTING_PUBLICATION_KEY] = record
    if store.get(ROUTING_PUBLICATION_KEY) != record:
        msg = "the routing publication record reads back differently"
        raise RoutingConflictError(msg)
