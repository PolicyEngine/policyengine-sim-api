"""Fixtures for gateway endpoint tests."""

from copy import deepcopy

import pytest

TEST_APP_RELEASE_BUNDLE = {
    "app_name": "policyengine-simulation-py4-10-0",
    "policyengine_version": "4.10.0",
    "us": {
        "model_version": "1.500.0",
        "data_version": "populace-us-2024-test",
        "data_artifact_revision": "us-artifact-revision",
        "default_dataset": "populace_us_2024",
        "default_dataset_uri": "hf://policyengine/populace-us/populace_us_2024.h5@us-artifact-revision",
        "dataset_uris": {
            "populace_us_2024": "hf://policyengine/populace-us/populace_us_2024.h5@us-artifact-revision",
        },
    },
    "uk": {
        "model_version": "2.66.0",
        "data_version": "populace-uk-2023-test",
        "data_artifact_revision": "uk-artifact-revision",
        "default_dataset": "populace_uk_2023",
        "default_dataset_uri": "hf://policyengine/populace-uk-private/populace_uk_2023.h5@uk-artifact-revision",
        "dataset_uris": {
            "populace_uk_2023": "hf://policyengine/populace-uk-private/populace_uk_2023.h5@uk-artifact-revision",
        },
    },
}

TEST_APP_NAMES = (
    "policyengine-simulation-py4-10-0",
    "policyengine-simulation-py3-9-0",
)

TEST_ROUTING_STATE = {
    "schema_version": 1,
    "generation": "4.10.0:policyengine-simulation-py4-10-0",
    "latest": {
        "policyengine": "4.10.0",
        "us": "1.500.0",
        "uk": "2.66.0",
    },
    "routes": {
        "policyengine": {
            "4.10.0": "policyengine-simulation-py4-10-0",
            "3.9.0": "policyengine-simulation-py3-9-0",
        },
        "us": {
            "1.500.0": "policyengine-simulation-py4-10-0",
            "1.459.0": "policyengine-simulation-py3-9-0",
        },
        "uk": {
            "2.66.0": "policyengine-simulation-py4-10-0",
        },
    },
    "bundles": {
        "4.10.0": TEST_APP_RELEASE_BUNDLE,
        "3.9.0": {
            **TEST_APP_RELEASE_BUNDLE,
            "app_name": "policyengine-simulation-py3-9-0",
            "policyengine_version": "3.9.0",
            "us": {
                **TEST_APP_RELEASE_BUNDLE["us"],
                "model_version": "1.459.0",
            },
        },
    },
}


def _split_revision(dataset: str) -> tuple[str, str | None]:
    return dataset.rsplit("@", maxsplit=1) if "@" in dataset else (dataset, None)


def _runtime_dataset_uri(
    country_bundle: dict,
    dataset_uri: str,
    revision: str | None = None,
    use_bundle_default: bool = True,
) -> str:
    dataset_without_revision, existing_revision = _split_revision(dataset_uri)
    selected_revision = revision or existing_revision

    if dataset_without_revision.startswith("hf://policyengine/"):
        remainder = dataset_without_revision.removeprefix("hf://policyengine/")
        bucket, _, path = remainder.partition("/")
        if bucket.startswith("policyengine-") and "-data" in bucket:
            if (
                selected_revision == country_bundle.get("data_artifact_revision")
                and revision is None
            ):
                selected_revision = country_bundle["data_version"]
            dataset_without_revision = f"gs://{bucket}/{path}"

    if selected_revision is None and use_bundle_default:
        selected_revision = country_bundle["data_version"]

    if dataset_without_revision.startswith(("hf://", "gs://")):
        return f"{dataset_without_revision}@{selected_revision}"
    return dataset_uri


def resolve_test_dataset_uri(
    country: str,
    dataset: str | None,
    data_version: str | None = None,
) -> str | None:
    country_bundle = TEST_APP_RELEASE_BUNDLE[country]
    if dataset is None:
        return _runtime_dataset_uri(
            country_bundle,
            country_bundle["default_dataset_uri"],
            data_version,
        )
    if "://" in dataset:
        return _runtime_dataset_uri(
            country_bundle,
            dataset,
            data_version,
            use_bundle_default=dataset.startswith("hf://"),
        )

    dataset_name, revision = _split_revision(dataset)
    aliases = country_bundle.get("dataset_aliases")
    if isinstance(aliases, dict):
        dataset_name = aliases.get(dataset_name, dataset_name)
    dataset_uri = country_bundle["dataset_uris"].get(dataset_name, dataset_name)
    if revision is not None and dataset_uri == dataset_name:
        return dataset
    if dataset_uri == dataset_name:
        return dataset_uri
    return _runtime_dataset_uri(country_bundle, dataset_uri, revision or data_version)


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

    def keys(self):
        return self._data.keys()

    def items(self):
        return self._data.items()

    @classmethod
    def from_name(cls, name: str):
        """Mock from_name that returns a MockDict based on name."""
        raise NotImplementedError("Mock not configured")


class MockFunctionCall:
    """Mock for Modal FunctionCall returned by spawn."""

    registry = {}
    from_id_errors = {}

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
        if object_id in cls.from_id_errors:
            raise cls.from_id_errors[object_id]
        if object_id not in cls.registry:
            raise KeyError(object_id)
        return cls.registry[object_id]


class MockFunction:
    """Mock for Modal Function."""

    def __init__(self):
        self.last_payload = None
        self.last_from_name_call = None
        self.last_call = None
        self.calls = []

    def bind(self, app_name: str, func_name: str) -> "BoundMockFunction":
        return BoundMockFunction(self, app_name, func_name)

    def call_for(self, object_id: str) -> MockFunctionCall:
        call = MockFunctionCall(object_id=object_id)
        self.last_call = call
        return call


class BoundMockFunction:
    """Function handle returned by Modal.Function.from_name."""

    def __init__(self, recorder: MockFunction, app_name: str, func_name: str):
        self.recorder = recorder
        self.app_name = app_name
        self.func_name = func_name

    def spawn(self, payload: dict) -> MockFunctionCall:
        self.recorder.last_payload = payload
        is_batch = self.func_name == "run_budget_window_batch"
        object_id = "mock-batch-job-id-123" if is_batch else "mock-job-id-123"
        self.recorder.last_call = MockFunctionCall(object_id=object_id)
        if is_batch:
            self.recorder.last_call.running = True
        self.recorder.calls.append((self.app_name, self.func_name, payload, object_id))
        return self.recorder.last_call


class MockModalException:
    class NotFoundError(Exception):
        pass

    class OutputExpiredError(Exception):
        pass


@pytest.fixture
def mock_modal(monkeypatch):
    """Patch Modal calls in the gateway endpoints module."""
    from policyengine_simulation_contract import dataset_uri
    from policyengine_simulation_contract import budget_window_state
    from src.modal.gateway import endpoints

    mock_func = MockFunction()
    mock_dicts = {
        "simulation-api-policyengine-versions": {},
        "simulation-api-routing-state": {"active": deepcopy(TEST_ROUTING_STATE)},
    }
    MockFunctionCall.registry = {}
    MockFunctionCall.from_id_errors = {}

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
            return mock_func.bind(app_name, func_name)

    class MockModal:
        Dict = MockModalDict
        Function = MockModalFunction
        FunctionCall = MockFunctionCall
        exception = MockModalException

    monkeypatch.setattr(endpoints, "modal", MockModal)
    monkeypatch.setattr(budget_window_state, "modal", MockModal)
    monkeypatch.setattr(
        dataset_uri,
        "with_hf_revision",
        lambda dataset_uri, revision: (
            f"{dataset_uri.rsplit('@', maxsplit=1)[0]}@{revision}"
            if dataset_uri.startswith("hf://")
            else dataset_uri
        ),
    )
    monkeypatch.setattr(
        dataset_uri,
        "validate_hf_dataset_uri",
        lambda dataset_uri: dataset_uri,
    )

    return {
        "func": mock_func,
        "dicts": mock_dicts,
        "function_call": MockFunctionCall,
        "exception": MockModalException,
    }
