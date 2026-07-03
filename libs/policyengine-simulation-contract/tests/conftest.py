"""Fixtures for contract lib tests.

A minimal in-memory stand-in for the ``modal`` module: just enough for
budget_window_state's Dict round-trips. The executor project keeps its own
richer mock (fixtures/gateway/test_endpoints.py) for endpoint tests.
"""

import pytest

from policyengine_simulation_contract import budget_window_state


class MockDict:
    def __init__(self, store: dict):
        self._store = store

    def __getitem__(self, key):
        return self._store[key]

    def __setitem__(self, key, value):
        self._store[key] = value

    def __contains__(self, key):
        return key in self._store

    def get(self, key, default=None):
        return self._store.get(key, default)


@pytest.fixture
def mock_modal(monkeypatch):
    """Patch modal.Dict in budget_window_state with an in-memory store."""
    mock_dicts: dict[str, dict] = {}

    class MockModalDict:
        @staticmethod
        def from_name(name: str, create_if_missing: bool = False):
            if create_if_missing and name not in mock_dicts:
                mock_dicts[name] = {}
            if name not in mock_dicts:
                raise KeyError(f"Mock dict not configured for: {name}")
            return MockDict(mock_dicts[name])

    class MockModal:
        Dict = MockModalDict

    monkeypatch.setattr(budget_window_state, "modal", MockModal)
    return {"dicts": mock_dicts}
