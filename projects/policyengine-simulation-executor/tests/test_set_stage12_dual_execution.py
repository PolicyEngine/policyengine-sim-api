from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.modal.utils import set_stage12_dual_execution as command


class FakeStore(dict):
    pass


def _replace_modal_dict(monkeypatch, from_name) -> None:
    """Replace the external Modal dependency with a bounded test double."""
    monkeypatch.setattr(
        command,
        "modal",
        SimpleNamespace(Dict=SimpleNamespace(from_name=from_name)),
    )


def test_modal_and_logical_environments_are_kept_separate(monkeypatch) -> None:
    store = FakeStore()
    calls = []

    def from_name(name, *, environment_name, create_if_missing):
        calls.append((name, environment_name, create_if_missing))
        return store

    _replace_modal_dict(monkeypatch, from_name)

    command.set_dual_execution(
        modal_environment="staging",
        deployment_environment="staging",
        control_name="simulation-api-v2-dual-execution",
        manifest_selection_enabled=False,
        economy_enabled=False,
    )

    assert calls == [
        ("simulation-api-v2-dual-execution", "staging", True),
    ]
    assert store["active"]["environment"] == "staging"
    assert store["active"]["manifest_selection_enabled"] is False
    assert store["active"]["economy_enabled"] is False


def test_production_control_is_written_to_modal_main(monkeypatch) -> None:
    store = FakeStore()
    calls = []

    def from_name(name, *, environment_name, create_if_missing):
        calls.append((name, environment_name, create_if_missing))
        return store

    _replace_modal_dict(monkeypatch, from_name)

    command.set_dual_execution(
        modal_environment="main",
        deployment_environment="production",
        control_name="simulation-api-v2-dual-execution",
        manifest_selection_enabled=True,
        economy_enabled=True,
    )

    assert calls[0][1] == "main"
    assert store["active"]["environment"] == "production"


def test_control_rejects_a_github_environment_name(monkeypatch) -> None:
    _replace_modal_dict(
        monkeypatch,
        lambda *args, **kwargs: pytest.fail("Modal must not be called"),
    )

    with pytest.raises(ValueError, match="staging or production"):
        command.set_dual_execution(
            modal_environment="staging",
            deployment_environment="beta",
            control_name="simulation-api-v2-dual-execution",
            manifest_selection_enabled=False,
            economy_enabled=False,
        )
