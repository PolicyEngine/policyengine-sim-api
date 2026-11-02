"""Record, apply, complete, restore and finalize routing plans in Modal."""

from typing import TYPE_CHECKING

from src.modal.routing_publisher.plans import (
    RoutingConflictError,
    require_finished_publication,
    routing_state_digest,
    unfinished_publication,
    validate_routing_plan,
    write_publication_record,
)
from src.modal.routing_publisher.state import (
    ROUTING_PUBLICATION_KEY,
    ROUTING_STATE_ACTIVE_KEY,
)
from src.modal.routing_publisher.stores import routing_store
from src.modal.routing_publisher.writers import require_serialized_routing_workflow

if TYPE_CHECKING:
    import modal

# Raised, as a RuntimeError, when a routing write or a plan's completion finds
# no routing-state Dict to write to.
UNOPENED_ROUTING_STORE = (
    "the routing state Modal Dict could not be opened; nothing was written"
)


def record_routing_plan(*, environment: str, plan: dict) -> None:
    """Journal a pending release before its artifact is uploaded or routing changes.

    Its finalizer consumes this durable plan even if the runner loses the local
    file or uploading its artifact fails. Operator recovery can restore a plan
    if the finalizer itself is lost.
    """
    require_serialized_routing_workflow()
    validate_routing_plan(plan)
    store = routing_store(environment, create_if_missing=True)
    if store is None:
        raise RuntimeError(UNOPENED_ROUTING_STORE)
    require_finished_publication(store)
    if (
        routing_state_digest(store.get(ROUTING_STATE_ACTIVE_KEY))
        != plan["before_sha256"]
    ):
        msg = "the routing state changed before recording the plan"
        raise RoutingConflictError(msg)
    write_publication_record(store, plan=plan, status="pending")


def complete_routing_plan(*, environment: str, plan: dict) -> None:
    """Acknowledge the owning release only after all its gates have passed."""
    require_serialized_routing_workflow()
    validate_routing_plan(plan)
    store = routing_store(environment, create_if_missing=False)
    if store is None:
        raise RuntimeError(UNOPENED_ROUTING_STORE)
    if unfinished_publication(store) != plan:
        msg = "no matching unfinished routing publication to complete"
        raise RoutingConflictError(msg)
    if (
        routing_state_digest(store.get(ROUTING_STATE_ACTIVE_KEY))
        != plan["after_sha256"]
    ):
        msg = "the completed release's routing state is not live"
        raise RoutingConflictError(msg)
    write_publication_record(store, plan=plan, status="completed")


def apply_routing_plan(*, environment: str, plan: dict) -> None:
    """Publish ``plan``'s state if the live state is still its starting point."""
    require_serialized_routing_workflow()
    validate_routing_plan(plan)
    store = routing_store(environment, create_if_missing=True)
    if store is None:
        raise RuntimeError(UNOPENED_ROUTING_STORE)
    require_finished_publication(store, allowed_plans=[plan])
    current = store.get(ROUTING_STATE_ACTIVE_KEY)
    if routing_state_digest(current) != plan["before_sha256"]:
        msg = (
            "the routing state changed after this release was planned; "
            "nothing was published"
        )
        raise RoutingConflictError(msg)
    store[ROUTING_STATE_ACTIVE_KEY] = plan["after"]
    written = store.get(ROUTING_STATE_ACTIVE_KEY)
    if routing_state_digest(written) != plan["after_sha256"]:
        msg = "the published routing state reads back differently"
        raise RoutingConflictError(msg)


def restore_routing_plans(*, environment: str, plans: list[dict]) -> str:
    """Undo a deployment's plans (one per run attempt): ``restored`` or ``unchanged``.

    Repeatedly restores the starting state of a plan whose published state is
    live, newest attempt first, skipping plans that changed nothing, until no
    plan's published state is live. ``unchanged`` means the live state is a
    starting state no plan replaced (nothing was applied, or it was already
    undone). Any other live state is a conflict and nothing is changed.
    """
    require_serialized_routing_workflow()
    for plan in plans:
        validate_routing_plan(plan)
    if not plans:
        msg = "no routing plan to restore"
        raise ValueError(msg)
    store = routing_store(environment, create_if_missing=False)
    require_finished_publication(store, allowed_plans=plans)
    pending = unfinished_publication(store)
    if store is None:
        if all(plan["before"] is None for plan in plans):
            return "unchanged"
        msg = "there is no routing state to restore into"
        raise RoutingConflictError(msg)
    outcome = _restore_live_plans(store, plans)
    if pending is not None:
        write_publication_record(store, plan=pending, status="restored")
    return outcome


def _restore_live_plans(store: "modal.Dict", plans: list[dict]) -> str:
    """Restore each live plan's starting state, newest attempt first.

    Returns ``restored``, or ``unchanged`` when the live state is a starting
    state no plan replaced; any other live state is a conflict.
    """
    remaining = [
        plan for plan in plans if plan["before_sha256"] != plan["after_sha256"]
    ]
    outcome = "unchanged"
    while True:
        digest = routing_state_digest(store.get(ROUTING_STATE_ACTIVE_KEY))
        live = [plan for plan in remaining if plan["after_sha256"] == digest]
        if not live:
            break
        plan = live[-1]
        remaining.remove(plan)
        _write_starting_state(store, plan)
        outcome = "restored"
    if outcome == "unchanged" and not any(
        plan["before_sha256"] == digest for plan in plans
    ):
        msg = (
            "the routing state is neither what this release replaced nor what "
            "it published; another change was made, so nothing was restored"
        )
        raise RoutingConflictError(msg)
    return outcome


def _write_starting_state(store: "modal.Dict", plan: dict) -> None:
    """Put back the state ``plan`` replaced, and check that it reads back."""
    if plan["before"] is None:
        store.pop(ROUTING_STATE_ACTIVE_KEY)
    else:
        store[ROUTING_STATE_ACTIVE_KEY] = plan["before"]
    if (
        routing_state_digest(store.get(ROUTING_STATE_ACTIVE_KEY))
        != plan["before_sha256"]
    ):
        msg = "the restored routing state reads back differently"
        raise RoutingConflictError(msg)


def restore_routing_plan(*, environment: str, plan: dict) -> str:
    """Undo one plan: ``restored``, ``unchanged`` (never applied) or a conflict."""
    require_serialized_routing_workflow()
    return restore_routing_plans(environment=environment, plans=[plan])


# A workflow run's publication id is "<run id>-<run attempt>-<environment>".
_RUN_PUBLICATION_ID_PARTS = 3


def _publication_owned_by_release(
    plan: dict, *, publication_run: str | None, release_environment: str
) -> bool:
    """Match a workflow run across attempts, or the operator's selected store."""
    if publication_run is None:
        return True

    parts = str(plan.get("publication_id", "")).split("-")
    return (
        len(parts) == _RUN_PUBLICATION_ID_PARTS
        and parts[1].isdigit()
        and parts[2] == release_environment
        and parts[0] == publication_run
    )


def finalize_routing_publication(
    *,
    environment: str,
    release_environment: str,
    gates_passed: bool,
    publication_run: str | None = None,
) -> str:
    """Finish a release using its durable journal, without a plan artifact.

    A failed-jobs rerun owns every attempt of the same run and environment.
    Successful gates complete only the plan's live published state; every
    other owned pending publication is restored. Conflicts leave it pending.
    Omitting the run selects operator recovery in the chosen Modal store,
    which can only restore and does not depend on the publication id's format.
    Already finished records owned by this run return their recorded outcome
    without writing, so retrying a finalizer after output loss is safe.
    """
    require_serialized_routing_workflow()
    if release_environment not in {"beta", "prod"}:
        msg = "release_environment must be beta or prod"
        raise ValueError(msg)
    if publication_run is None and gates_passed:
        msg = "operator recovery requires gates_passed=false"
        raise ValueError(msg)
    store = routing_store(environment, create_if_missing=False)
    if store is None:
        # No routing-state Dict, so nothing was ever published or recorded.
        return "no-pending"
    pending = unfinished_publication(store)
    if pending is None:
        record = store.get(ROUTING_PUBLICATION_KEY)
        if (
            record is not None
            and publication_run is not None
            and _publication_owned_by_release(
                record["plan"],
                publication_run=publication_run,
                release_environment=release_environment,
            )
            and (
                record["status"] != "completed"
                or routing_state_digest(store.get(ROUTING_STATE_ACTIVE_KEY))
                == record["plan"]["after_sha256"]
            )
        ):
            return record["status"]
        return "no-pending"
    if not _publication_owned_by_release(
        pending,
        publication_run=publication_run,
        release_environment=release_environment,
    ):
        return "foreign-publication"
    if (
        gates_passed
        and routing_state_digest(store.get(ROUTING_STATE_ACTIVE_KEY))
        == pending["after_sha256"]
    ):
        complete_routing_plan(environment=environment, plan=pending)
        return "completed"
    restore_routing_plan(environment=environment, plan=pending)
    return "restored"
