"""Fixtures for gateway endpoint tests."""

from collections.abc import ItemsView, KeysView
from copy import deepcopy
from typing import ClassVar, Self

import pytest
from policyengine_simulation_contract.macro_output import (
    AgePovertyOutput,
    BaselineReformValue,
    BudgetaryImpact,
    DecileOutput,
    DetailedBudgetOutput,
    GenderPovertyOutput,
    InequalityOutput,
    IntraDecileOutput,
    PovertyByGenderOutput,
    PovertyOutput,
    SingleYearMacroOutput,
)

TEST_APP_RELEASE_BUNDLE = {
    "app_name": "policyengine-simulation-py4-10-0",
    "policyengine_version": "4.10.0",
    "us": {
        "model_version": "1.500.0",
        "data_package_version": "0.1.0",
        "data_version": "populace-us-2024-test",
        "data_artifact_revision": "us-artifact-revision",
        "default_dataset": "populace_us_2024",
        "default_dataset_uri": "hf://policyengine/populace-us/populace_us_2024.h5@us-artifact-revision",
        "dataset_uris": {
            "populace_us_2024": "hf://policyengine/populace-us/populace_us_2024.h5@us-artifact-revision",
            "populace_us_2024_acs_local": "hf://policyengine/populace-us/populace_us_2024_acs_local.h5@us-local-revision",
            "calibration_diagnostics": "hf://policyengine/populace-us/calibration_diagnostics.json@us-artifact-revision",
        },
    },
    "uk": {
        "model_version": "2.66.0",
        "data_package_version": "0.1.0",
        "data_version": "populace-uk-2023-test",
        "data_artifact_revision": "uk-artifact-revision",
        "default_dataset": "populace_uk_2023",
        "default_dataset_uri": "hf://policyengine/populace-uk-private/populace_uk_2023.h5@uk-artifact-revision",
        "dataset_uris": {
            "populace_uk_2023": "hf://policyengine/populace-uk-private/populace_uk_2023.h5@uk-artifact-revision",
            "local_authority_weights": "hf://policyengine/policyengine-uk-data-private/local_authority_weights.h5@uk-artifact-revision",
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
    *,
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
                selected_revision = country_bundle.get(
                    "data_package_version", country_bundle["data_version"]
                )
            dataset_without_revision = f"gs://{bucket}/{path}"

    if selected_revision is None and use_bundle_default:
        selected_revision = country_bundle.get(
            "data_package_version", country_bundle["data_version"]
        )

    if dataset_without_revision.startswith(("hf://", "gs://")):
        return f"{dataset_without_revision}@{selected_revision}"
    return dataset_uri


def resolve_test_dataset_uri(
    country: str,
    dataset: str | None,
    data_version: str | None = None,
) -> str | None:
    """Return the dataset URI the gateway should report for this test request.

    Mirrors the gateway's resolution against ``TEST_APP_RELEASE_BUNDLE``:
    aliases and bundle dataset names map to pinned URIs, and an explicit
    revision or ``data_version`` overrides the bundle default.
    """
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

    def __init__(self, data: dict) -> None:
        """Wrap ``data`` without copying, so writes reach the test's dict."""
        self._data = data

    def __getitem__(self, key: str) -> object:
        """Return the stored value; raise KeyError like a Modal Dict."""
        if key not in self._data:
            raise KeyError(key)
        return self._data[key]

    def __setitem__(self, key: str, value: object) -> None:
        """Store a value in the wrapped dict."""
        self._data[key] = value

    def get(self, key: str, default: object = None) -> object:
        """Return the stored value, or ``default`` when the key is absent."""
        return self._data.get(key, default)

    def keys(self) -> KeysView[str]:
        """Return a live view of the stored keys."""
        return self._data.keys()

    def items(self) -> ItemsView[str, object]:
        """Return a live view of the stored items."""
        return self._data.items()

    @classmethod
    def from_name(cls, name: str) -> Self:
        """Mock from_name that returns a MockDict based on name."""
        msg = "Mock not configured"
        raise NotImplementedError(msg)


class MockFunctionCall:
    """Mock for Modal FunctionCall returned by spawn."""

    registry: ClassVar[dict[str, "MockFunctionCall"]] = {}
    from_id_errors: ClassVar[dict[str, BaseException]] = {}

    def __init__(self, object_id: str = "mock-job-id-123") -> None:
        """Register a complete call whose result is a valid zeroed macro output.

        Tests set ``running`` or ``error`` afterwards to change what get returns.
        """
        self.object_id = object_id
        value = BaselineReformValue(baseline=0.0, reform=0.0)
        age_poverty = AgePovertyOutput(
            child=value,
            adult=value,
            senior=value,
            all=value,
        )
        gender_poverty = GenderPovertyOutput(male=value, female=value)
        self.result = SingleYearMacroOutput(
            model_version="1.500.0",
            data_version="1.0.0",
            budget=BudgetaryImpact(
                tax_revenue_impact=0.0,
                state_tax_revenue_impact=0.0,
                benefit_spending_impact=0.0,
                budgetary_impact=0.0,
                households=0.0,
                baseline_net_income=0.0,
            ),
            detailed_budget=DetailedBudgetOutput({}),
            decile=DecileOutput(average={}, relative={}),
            inequality=InequalityOutput(
                gini=value,
                top_10_pct_share=value,
                top_1_pct_share=value,
            ),
            poverty=PovertyOutput(
                poverty=age_poverty,
                deep_poverty=age_poverty,
            ),
            poverty_by_gender=PovertyByGenderOutput(
                poverty=gender_poverty,
                deep_poverty=gender_poverty,
            ),
            poverty_by_race=None,
            intra_decile=IntraDecileOutput(deciles={}, all={}),
            wealth_decile=None,
            intra_wealth_decile=None,
            labor_supply_response=None,
            constituency_impact=None,
            local_authority_impact=None,
            congressional_district_impact=None,
            cliff_impact=None,
        ).model_dump(mode="json")
        self.error = None
        self.running = False
        self.__class__.registry[object_id] = self

    def get(self, timeout: int = 0) -> object:  # noqa: ARG002  # mirrors modal.FunctionCall.get
        """Return the result; raise TimeoutError while running, or the set error."""
        if self.running:
            raise TimeoutError
        if self.error is not None:
            raise self.error
        return self.result

    @classmethod
    def from_id(cls, object_id: str) -> "MockFunctionCall":
        """Return a registered call, raising any error staged for that ID.

        Raise KeyError for an unknown ID.
        """
        if object_id in cls.from_id_errors:
            raise cls.from_id_errors[object_id]
        if object_id not in cls.registry:
            raise KeyError(object_id)
        return cls.registry[object_id]


class MockFunction:
    """Mock for Modal Function."""

    def __init__(self) -> None:
        """Start with no recorded spawns or lookups."""
        self.last_payload = None
        self.last_observability_context = None
        self.last_from_name_call = None
        self.last_call = None
        self.calls = []

    def bind(self, app_name: str, func_name: str) -> "BoundMockFunction":
        """Return a handle for one app function that records into this mock."""
        return BoundMockFunction(self, app_name, func_name)

    def call_for(self, object_id: str) -> MockFunctionCall:
        """Register a completed call under ``object_id`` and make it the last call."""
        call = MockFunctionCall(object_id=object_id)
        self.last_call = call
        return call


class BoundMockFunction:
    """Function handle returned by Modal.Function.from_name."""

    def __init__(self, recorder: MockFunction, app_name: str, func_name: str) -> None:
        """Bind the app and function names that spawns will be recorded under."""
        self.recorder = recorder
        self.app_name = app_name
        self.func_name = func_name

    def spawn(
        self,
        payload: dict,
        *,
        observability_context: dict | None = None,
    ) -> MockFunctionCall:
        """Record the spawn and return a call with a fixed ID.

        A ``run_budget_window_batch`` spawn gets the batch ID and stays running;
        any other spawn completes immediately.
        """
        self.recorder.last_payload = payload
        self.recorder.last_observability_context = observability_context
        is_batch = self.func_name == "run_budget_window_batch"
        object_id = "mock-batch-job-id-123" if is_batch else "mock-job-id-123"
        self.recorder.last_call = MockFunctionCall(object_id=object_id)
        if is_batch:
            self.recorder.last_call.running = True
        self.recorder.calls.append(
            (
                self.app_name,
                self.func_name,
                payload,
                observability_context,
                object_id,
            )
        )
        return self.recorder.last_call


class MockModalException:
    """Stand-in for ``modal.exception`` with the error types the gateway catches."""

    class NotFoundError(Exception):
        """Raised for a function call Modal has never heard of."""

    class OutputExpiredError(Exception):
        """Raised for a function call whose result Modal no longer keeps."""


@pytest.fixture
def mock_modal(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Patch Modal calls in the gateway endpoints module."""
    from policyengine_simulation_contract import (  # noqa: PLC0415  # patch targets, imported when the fixture runs
        budget_window_state,
        dataset_uri,
        job_progress_state,
    )
    from policyengine_simulation_gateway import (  # noqa: PLC0415  # patch targets, imported when the fixture runs
        endpoints,
    )

    mock_func = MockFunction()
    mock_dicts = {
        "simulation-api-policyengine-versions": {},
        "simulation-api-routing-state": {"active": deepcopy(TEST_ROUTING_STATE)},
    }
    MockFunctionCall.registry = {}
    MockFunctionCall.from_id_errors = {}

    class MockModalDict:
        @staticmethod
        def from_name(name: str, *, create_if_missing: bool = False) -> MockDict:
            if create_if_missing and name not in mock_dicts:
                mock_dicts[name] = {}
            if name not in mock_dicts:
                msg = f"Mock dict not configured for: {name}"
                raise KeyError(msg)
            return MockDict(mock_dicts[name])

    class MockModalFunction:
        @staticmethod
        def from_name(app_name: str, func_name: str) -> BoundMockFunction:
            mock_func.last_from_name_call = (app_name, func_name)
            return mock_func.bind(app_name, func_name)

    class MockModal:
        Dict = MockModalDict
        Function = MockModalFunction
        FunctionCall = MockFunctionCall
        exception = MockModalException

    monkeypatch.setattr(endpoints, "modal", MockModal)
    monkeypatch.setattr(budget_window_state, "modal", MockModal)
    monkeypatch.setattr(job_progress_state, "modal", MockModal)
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
