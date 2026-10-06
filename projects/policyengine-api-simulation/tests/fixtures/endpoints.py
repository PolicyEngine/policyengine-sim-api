"""
Fixtures for gateway endpoints tests.

Provides mocks for Modal Dict and Function classes.
"""

import pytest


class MockDict:
    """Mock for Modal.Dict to simulate version registry."""

    def __init__(self, data: dict):
        self._data = data

    def __getitem__(self, key: str):
        if key not in self._data:
            raise KeyError(key)
        return self._data[key]

    def __setitem__(self, key: str, value):
        self._data[key] = value

    def get(self, key: str, default=None):
        return self._data.get(key, default)

    @classmethod
    def from_name(cls, name: str):
        """Mock from_name that returns a MockDict based on name."""
        # This will be overridden in tests
        raise NotImplementedError("Mock not configured")


class MockFunctionCall:
    """Mock for Modal FunctionCall returned by spawn."""

    registry = {}

    def __init__(self, object_id: str = "mock-job-id-123"):
        self.object_id = object_id
        self.result = {"budget": {"total": 1000000}}
        self.error = None
        self.running = False
        self.__class__.registry[object_id] = self

    def get(self, timeout: int = 0):
        if self.running:
            raise TimeoutError()
        if self.error is not None:
            raise self.error
        return self.result

    @classmethod
    def from_id(cls, object_id: str):
        if object_id not in cls.registry:
            raise KeyError(object_id)
        return cls.registry[object_id]


class MockFunction:
    """Mock for Modal Function."""

    def __init__(self):
        self.last_payload = None
        self.last_from_name_call = None
        self.last_call = None

    def spawn(self, payload: dict) -> MockFunctionCall:
        self.last_payload = payload
        self.last_call = MockFunctionCall()
        return self.last_call

    @classmethod
    def from_name(cls, app_name: str, func_name: str):
        """Mock from_name that returns a MockFunction."""
        # This will be overridden in tests
        raise NotImplementedError("Mock not configured")


@pytest.fixture
def mock_modal(monkeypatch):
    """
    Fixture that patches the modal library calls in the endpoints module.

    Returns a dict with mock objects that tests can configure.

    Note: pytest's pythonpath config adds 'src' to path, causing 'import modal'
    to find our local src/modal package. We patch at the module level in endpoints.

    Usage:
        def test_something(mock_modal, client):
            mock_modal["dicts"]["simulation-api-us-versions"] = {
                "latest": "1.500.0",
                "1.500.0": "policyengine-simulation-us1-500-0-uk2-66-0",
            }
            # ... test code ...
    """
    from src.modal.gateway import endpoints

    # Create mock objects
    mock_func = MockFunction()
    mock_dicts = {}
    MockFunctionCall.registry = {}

    class MockModalDict:
        @staticmethod
        def from_name(name: str, create_if_missing: bool = False):
            if create_if_missing and name not in mock_dicts:
                mock_dicts[name] = {}
            if name not in mock_dicts:
                raise KeyError(f"Mock dict not configured for: {name}")
            return MockDict(mock_dicts[name])

    class MockModalFunction:
        @staticmethod
        def from_name(app_name: str, func_name: str):
            mock_func.last_from_name_call = (app_name, func_name)
            return mock_func

    # Create a mock modal module object
    class MockModal:
        Dict = MockModalDict
        Function = MockModalFunction
        FunctionCall = MockFunctionCall

    # Patch the modal import in the endpoints module
    monkeypatch.setattr(endpoints, "modal", MockModal)

    return {
        "func": mock_func,
        "dicts": mock_dicts,
    }
