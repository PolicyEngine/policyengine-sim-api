"""Open the routing Modal Dicts, and write whole routing states to them."""

from policyengine_simulation_contract.simulation_workers import SIMULATION_WORKER_CLASS
from src.modal.routing_publisher.legacy import (
    build_legacy_seed_routing_state,
    merge_legacy_seed_into_current,
)
from src.modal.routing_publisher.plans import require_finished_publication
from src.modal.routing_publisher.state import (
    APP_RELEASE_BUNDLES_DICT_NAME,
    POLICYENGINE_VERSION_DICT_NAME,
    ROUTING_STATE_ACTIVE_KEY,
    ROUTING_STATE_DICT_NAME,
    UK_VERSION_DICT_NAME,
    US_VERSION_DICT_NAME,
    DeployedAppVersions,
    RoutingState,
    build_next_routing_state,
    build_restored_routing_state,
    coerce_routing_state,
)
from src.modal.routing_publisher.writers import require_serialized_routing_workflow

import modal


def routing_store(environment: str, *, create_if_missing: bool) -> "modal.Dict | None":
    """Return the routing-state Dict, or None when it doesn't exist.

    modal's Dict.from_name returns a handle without contacting Modal, so a
    missing Dict would surface as NotFoundError at the first read. When the
    Dict must already exist, look it up now, so a missing one is reported here.
    """
    try:
        store = modal.Dict.from_name(
            ROUTING_STATE_DICT_NAME,
            environment_name=environment,
            create_if_missing=create_if_missing,
        )
        if not create_if_missing:
            store.hydrate()
    except KeyError:
        return None
    except Exception as exc:
        if exc.__class__.__name__ == "NotFoundError":
            return None
        raise
    return store


class LatestAppWithoutWorkerClassError(ValueError):
    """A routing change would route ``latest`` to an app without the worker class."""


def require_latest_app_defines_worker_class(state: object, *, environment: str) -> None:
    """Refuse a routing state whose ``latest`` app can't run comparison requests.

    The gateway spawns comparison requests only through an app's
    ``SimulationWorker`` class, which apps deployed before policyengine.py
    6.20.0 don't define, so routing ``latest`` to one of them would fail every
    unpinned comparison request. Raises ``LatestAppWithoutWorkerClassError``
    when Modal finds no such class in the app, or no such app. A state that
    routes ``latest`` to no app is not this check's concern.
    """
    routing = coerce_routing_state(state)
    version = routing["latest"].get("policyengine")
    app_name = routing["routes"]["policyengine"].get(version) if version else None
    if not app_name:
        return
    worker_class = modal.Cls.from_name(
        app_name, SIMULATION_WORKER_CLASS, environment_name=environment
    )
    try:
        worker_class.hydrate()
    except Exception as exc:
        if exc.__class__.__name__ != "NotFoundError":
            raise
        msg = (
            f"policyengine {version} routes to {app_name}, which defines no "
            f"{SIMULATION_WORKER_CLASS} class in Modal environment {environment}. "
            "The gateway runs comparison requests only through that class, which "
            "apps deployed before policyengine.py 6.20.0 don't define, so routing "
            "latest there would fail every unpinned comparison request; routing "
            "is unchanged"
        )
        raise LatestAppWithoutWorkerClassError(msg) from exc


def modal_dict_snapshot(
    *,
    name: str,
    environment: str,
    create_if_missing: bool = False,
) -> dict:
    """Return a copy of a Modal Dict's items, or an empty dict when it doesn't exist."""
    try:
        store = modal.Dict.from_name(
            name,
            environment_name=environment,
            create_if_missing=create_if_missing,
        )
    except KeyError:
        return {}
    except Exception as exc:
        if exc.__class__.__name__ == "NotFoundError":
            return {}
        raise
    try:
        return dict(store.items())
    except Exception as exc:
        if exc.__class__.__name__ == "NotFoundError":
            return {}
        raise


def seed_active_routing_state_from_legacy(*, environment: str) -> RoutingState:
    """Seed the active routing state from the legacy Dicts and store it.

    Routes and ``latest`` entries already in the active state win over the
    legacy seed.
    """
    require_serialized_routing_workflow()
    store = modal.Dict.from_name(
        ROUTING_STATE_DICT_NAME,
        environment_name=environment,
        create_if_missing=True,
    )
    require_finished_publication(store)
    legacy_seed = build_legacy_seed_routing_state(
        policyengine_versions=modal_dict_snapshot(
            name=POLICYENGINE_VERSION_DICT_NAME,
            environment=environment,
        ),
        us_versions=modal_dict_snapshot(
            name=US_VERSION_DICT_NAME,
            environment=environment,
        ),
        uk_versions=modal_dict_snapshot(
            name=UK_VERSION_DICT_NAME,
            environment=environment,
        ),
        app_release_bundles=modal_dict_snapshot(
            name=APP_RELEASE_BUNDLES_DICT_NAME,
            environment=environment,
        ),
    )
    next_state = merge_legacy_seed_into_current(
        current_state=store.get(ROUTING_STATE_ACTIVE_KEY),
        legacy_seed=legacy_seed,
    )
    store[ROUTING_STATE_ACTIVE_KEY] = next_state
    return next_state


def publish_routing_state(
    *,
    environment: str,
    deployed: DeployedAppVersions,
    force_latest: bool = False,
) -> RoutingState:
    """Add a deployed app to the active routing state and store it in one write."""
    require_serialized_routing_workflow()
    store = modal.Dict.from_name(
        ROUTING_STATE_DICT_NAME,
        environment_name=environment,
        create_if_missing=True,
    )
    require_finished_publication(store)
    next_state = build_next_routing_state(
        current_state=store.get(ROUTING_STATE_ACTIVE_KEY),
        deployed=deployed,
        force_latest=force_latest,
    )
    store[ROUTING_STATE_ACTIVE_KEY] = next_state
    return next_state


def read_latest_policyengine_version(*, environment: str) -> str | None:
    """Return the policyengine.py version unpinned requests are routed to now."""
    snapshot = modal_dict_snapshot(
        name=ROUTING_STATE_DICT_NAME, environment=environment
    )
    state = coerce_routing_state(snapshot.get(ROUTING_STATE_ACTIVE_KEY))
    return state["latest"].get("policyengine")


def restore_routing_state(
    *,
    environment: str,
    policyengine_version: str,
    expected_latest: str | None = None,
) -> RoutingState:
    """Point ``latest`` back at a published policyengine.py version and store it.

    Refuses, writing nothing, a version whose app defines no worker class
    (:func:`require_latest_app_defines_worker_class`).
    """
    require_serialized_routing_workflow()
    store = modal.Dict.from_name(
        ROUTING_STATE_DICT_NAME,
        environment_name=environment,
        create_if_missing=False,
    )
    require_finished_publication(store)
    next_state = build_restored_routing_state(
        current_state=store.get(ROUTING_STATE_ACTIVE_KEY),
        policyengine_version=policyengine_version,
        expected_latest=expected_latest,
    )
    require_latest_app_defines_worker_class(next_state, environment=environment)
    store[ROUTING_STATE_ACTIVE_KEY] = next_state
    return next_state
