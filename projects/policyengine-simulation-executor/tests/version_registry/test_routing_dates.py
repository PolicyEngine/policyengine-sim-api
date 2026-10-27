"""When each policyengine route was published and when latest last changed."""

from __future__ import annotations

from registry_test_support import (
    install_bundle_manifests,
    versioned_app_name,
)
from src.modal.routing_publisher import state as routing_state
from src.modal.utils import update_version_registry as registry

FIRST = "2026-10-01T09:00:00Z"
SECOND = "2026-10-08T09:00:00Z"
THIRD = "2026-10-15T09:00:00Z"

MODELS = {
    "6.9.0": ("2.1.0", "2.120.0"),
    "6.10.0": ("2.2.1", "2.132.6"),
    "6.10.1": ("2.2.1", "2.133.0"),
}


def _publish(monkeypatch, state, version, now, *, force_latest=False):
    install_bundle_manifests(monkeypatch, MODELS)
    us_version, uk_version = MODELS[version]
    return registry.build_next_routing_state(
        current_state=state,
        deployed=registry.DeployedAppVersions(
            app_name=versioned_app_name(version),
            policyengine_version=version,
            us_version=us_version,
            uk_version=uk_version,
        ),
        force_latest=force_latest,
        now=now,
    )


def test_a_first_publish_records_both_dates(monkeypatch):
    state = _publish(monkeypatch, None, "6.10.0", FIRST)

    assert state["published_at"] == {"6.10.0": FIRST}
    assert state["latest_since"] == FIRST


def test_republishing_a_version_keeps_its_dates(monkeypatch):
    state = _publish(monkeypatch, None, "6.10.0", FIRST)

    state = _publish(monkeypatch, state, "6.10.0", SECOND)

    assert state["published_at"] == {"6.10.0": FIRST}
    assert state["latest_since"] == FIRST


def test_a_newer_version_moves_latest_since(monkeypatch):
    state = _publish(monkeypatch, None, "6.10.0", FIRST)

    state = _publish(monkeypatch, state, "6.10.1", SECOND)

    assert state["published_at"] == {"6.10.0": FIRST, "6.10.1": SECOND}
    assert state["latest_since"] == SECOND


def test_an_older_version_is_published_without_moving_latest(monkeypatch):
    state = _publish(monkeypatch, None, "6.10.0", FIRST)

    state = _publish(monkeypatch, state, "6.9.0", SECOND)

    assert state["latest"]["policyengine"] == "6.10.0"
    assert state["published_at"]["6.9.0"] == SECOND
    assert state["latest_since"] == FIRST


def test_a_forced_older_latest_moves_latest_since(monkeypatch):
    state = _publish(monkeypatch, None, "6.10.0", FIRST)

    state = _publish(monkeypatch, state, "6.9.0", SECOND, force_latest=True)

    assert state["latest"]["policyengine"] == "6.9.0"
    assert state["latest_since"] == SECOND


def test_a_state_without_dates_gets_no_wrong_date_for_what_it_already_routes(
    monkeypatch,
):
    state = _publish(monkeypatch, None, "6.10.0", FIRST)
    del state["published_at"]
    del state["latest_since"]

    state = _publish(monkeypatch, state, "6.10.0", SECOND)

    assert "published_at" not in state
    assert "latest_since" not in state

    state = _publish(monkeypatch, state, "6.10.1", THIRD)

    assert state["published_at"] == {"6.10.1": THIRD}
    assert state["latest_since"] == THIRD


def test_a_rollback_moves_latest_since(monkeypatch):
    state = _publish(monkeypatch, None, "6.10.0", FIRST)
    state = _publish(monkeypatch, state, "6.10.1", SECOND)

    restored = routing_state.build_restored_routing_state(
        current_state=state,
        policyengine_version="6.10.0",
        expected_latest="6.10.1",
        now=THIRD,
    )

    assert restored["latest"]["policyengine"] == "6.10.0"
    assert restored["latest_since"] == THIRD
    assert restored["published_at"] == {"6.10.0": FIRST, "6.10.1": SECOND}


def test_coerce_keeps_valid_dates_and_drops_malformed_ones():
    coerced = routing_state.coerce_routing_state(
        {
            "published_at": {"6.10.0": FIRST, "6.9.0": 5, 7: SECOND},
            "latest_since": "",
        }
    )

    assert coerced["published_at"] == {"6.10.0": FIRST}
    assert "latest_since" not in coerced


def test_utc_now_is_an_iso_utc_time():
    assert routing_state.utc_now().endswith("Z")
    assert len(routing_state.utc_now()) == len(FIRST)
