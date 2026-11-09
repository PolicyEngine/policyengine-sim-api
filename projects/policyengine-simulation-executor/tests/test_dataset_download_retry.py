"""Tests for retrying a run-time dataset load after a transient download failure.

The downloads below run policyengine.py's own bundle download
(``_reuse_or_download_bundle_files``) against a fake ``requests.get``, so the
retry is checked against the errors policyengine.py actually raises.
"""

import hashlib
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests
from policyengine.provenance import dataset_materialization
from policyengine.provenance.dataset_materialization import (
    DatasetMaterializationError,
    _BundleDatasetSpec,
    _reuse_or_download_bundle_files,
)
from policyengine_simulation_executor import dataset_download_retry
from policyengine_simulation_executor.dataset_download_retry import (
    MAXIMUM_LOAD_ATTEMPTS,
    RetryTiming,
    load_with_download_retry,
)
from policyengine_simulation_executor.simulation_runtime import (
    DatasetSelection,
    _load_dataset_retrying_downloads,
)

DATASET_BYTES = b"complete dataset contents" * 1000
DATASET_SHA256 = hashlib.sha256(DATASET_BYTES).hexdigest()


class _RecordedTiming:
    """Retry timing that records each wait instead of sleeping."""

    def __init__(self):
        self.waits = []
        self.delay_ranges = []

    def timing(self) -> RetryTiming:
        def choose_delay(lower, upper):
            self.delay_ranges.append((lower, upper))
            return upper

        return RetryTiming(sleep=self.waits.append, choose_delay=choose_delay)


class _FakeResponse:
    """A streamed response that sends ``chunks``, raising any exception among them."""

    def __init__(self, status_code, chunks=()):
        self.status_code = status_code
        self._chunks = chunks

    def __enter__(self):
        return self

    def __exit__(self, *_exc_info):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:  # HTTP error statuses start at 400
            message = f"{self.status_code} error"
            raise requests.HTTPError(message, response=self)

    def iter_content(self, chunk_size):
        del chunk_size
        for chunk in self._chunks:
            if isinstance(chunk, BaseException):
                raise chunk
            yield chunk


def _bundle_dataset(destination: Path, *, sha256=DATASET_SHA256) -> _BundleDatasetSpec:
    return _BundleDatasetSpec(
        country_id="uk",
        dataset="uk_local_areas",
        data_package_name="policyengine-uk-data",
        repo_id="policyengine/policyengine-uk-data-private",
        repo_type="model",
        path="local_areas.h5",
        revision="1.0.0",
        sha256=sha256,
        destination=destination,
    )


def _download_with_responses(
    monkeypatch, tmp_path, responses, *, sha256=DATASET_SHA256
):
    """Download one bundle file through policyengine.py, retrying, with fake responses.

    Returns what happened: the requests made, the waits, and the file left.
    """
    remaining = list(responses)
    requested = []

    def fake_get(url, **_kwargs):
        requested.append(url)
        return remaining.pop(0)

    monkeypatch.setattr(dataset_materialization.requests, "get", fake_get)
    destination = tmp_path / "data" / "local_areas.h5"
    recorded = _RecordedTiming()
    outcome = SimpleNamespace(requested=requested, waits=recorded.waits, error=None)
    try:
        outcome.materialized = load_with_download_retry(
            lambda: _reuse_or_download_bundle_files(
                _bundle_dataset(destination, sha256=sha256)
            ),
            dataset_name="uk_local_areas",
            timing=recorded.timing(),
        )
    except Exception as error:  # noqa: BLE001  # the test inspects whatever was raised
        outcome.error = error
    outcome.destination = destination
    outcome.leftover_files = sorted(
        path.name for path in destination.parent.iterdir() if path != destination
    )
    return outcome


def test_a_read_timeout_while_streaming_is_retried_without_keeping_the_partial_file(
    monkeypatch, tmp_path
):
    # requests raises a read timeout in the middle of a streamed body as a
    # ConnectionError, as in release run 42040665530.
    read_timeout = requests.ConnectionError(
        "HTTPSConnectionPool(host='us.aws.cdn.hf.co', port=443): Read timed out."
    )
    outcome = _download_with_responses(
        monkeypatch,
        tmp_path,
        [
            _FakeResponse(200, [DATASET_BYTES[:100], read_timeout]),
            _FakeResponse(200, [DATASET_BYTES]),
        ],
    )

    assert outcome.error is None
    assert len(outcome.requested) == 2  # one failed attempt, one success
    assert outcome.destination.read_bytes() == DATASET_BYTES
    assert outcome.leftover_files == []
    assert outcome.waits == [dataset_download_retry.INITIAL_RETRY_DELAY_SECONDS]


@pytest.mark.parametrize(
    "first_response",
    [
        _FakeResponse(
            200, [requests.exceptions.ChunkedEncodingError("response ended early")]
        ),
        _FakeResponse(500),
        _FakeResponse(502),
        _FakeResponse(503),
        _FakeResponse(504),
    ],
    ids=["body-truncated", "500", "502", "503", "504"],
)
def test_a_truncated_body_or_server_error_is_retried(
    monkeypatch, tmp_path, first_response
):
    outcome = _download_with_responses(
        monkeypatch, tmp_path, [first_response, _FakeResponse(200, [DATASET_BYTES])]
    )

    assert outcome.error is None
    assert len(outcome.requested) == 2  # one failed attempt, one success
    assert outcome.destination.read_bytes() == DATASET_BYTES


@pytest.mark.parametrize(
    "request_error",
    [
        requests.ConnectionError("connection refused"),
        requests.exceptions.ConnectTimeout("connect timed out"),
        requests.exceptions.ReadTimeout("read timed out waiting for headers"),
    ],
    ids=["connection-refused", "connect-timeout", "read-timeout"],
)
def test_a_connection_error_or_timeout_before_a_response_is_retried(
    monkeypatch, tmp_path, request_error
):
    calls = []

    def fake_get(url, **_kwargs):
        calls.append(url)
        if len(calls) == 1:
            raise request_error
        return _FakeResponse(200, [DATASET_BYTES])

    monkeypatch.setattr(dataset_materialization.requests, "get", fake_get)
    destination = tmp_path / "local_areas.h5"
    recorded = _RecordedTiming()

    load_with_download_retry(
        lambda: _reuse_or_download_bundle_files(_bundle_dataset(destination)),
        dataset_name="uk_local_areas",
        timing=recorded.timing(),
    )

    assert len(calls) == 2  # one failed attempt, one success
    assert destination.read_bytes() == DATASET_BYTES


@pytest.mark.parametrize("status_code", [401, 403, 404])
def test_a_rejected_or_missing_dataset_is_never_retried(
    monkeypatch, tmp_path, status_code
):
    outcome = _download_with_responses(
        monkeypatch,
        tmp_path,
        [_FakeResponse(status_code), _FakeResponse(200, [DATASET_BYTES])],
    )

    assert isinstance(outcome.error, DatasetMaterializationError)
    assert len(outcome.requested) == 1
    assert outcome.waits == []
    assert not outcome.destination.exists()


def test_a_sha256_mismatch_is_never_retried_and_leaves_no_file(monkeypatch, tmp_path):
    outcome = _download_with_responses(
        monkeypatch,
        tmp_path,
        [_FakeResponse(200, [b"wrong contents"]), _FakeResponse(200, [DATASET_BYTES])],
    )

    assert isinstance(outcome.error, DatasetMaterializationError)
    assert "sha256" in str(outcome.error)
    assert len(outcome.requested) == 1
    assert not outcome.destination.exists()
    assert outcome.leftover_files == []


def test_another_client_error_is_never_retried(monkeypatch, tmp_path):
    outcome = _download_with_responses(
        monkeypatch,
        tmp_path,
        [_FakeResponse(400), _FakeResponse(200, [DATASET_BYTES])],
    )

    assert isinstance(outcome.error, requests.HTTPError)
    assert len(outcome.requested) == 1


def test_an_error_that_is_not_a_download_failure_is_never_retried():
    attempts = []

    def load():
        attempts.append(1)
        msg = "Unsupported dataset"
        raise ValueError(msg)

    with pytest.raises(ValueError, match="Unsupported dataset"):
        load_with_download_retry(
            load, dataset_name="uk_local_areas", timing=_RecordedTiming().timing()
        )

    assert len(attempts) == 1


def test_retries_stop_after_the_attempt_limit_with_growing_waits(monkeypatch, tmp_path):
    outcome = _download_with_responses(
        monkeypatch,
        tmp_path,
        [_FakeResponse(503) for _ in range(MAXIMUM_LOAD_ATTEMPTS + 1)],
    )

    assert isinstance(outcome.error, requests.HTTPError)
    assert outcome.error.response.status_code == 503  # the last attempt's status
    assert len(outcome.requested) == MAXIMUM_LOAD_ATTEMPTS
    initial = dataset_download_retry.INITIAL_RETRY_DELAY_SECONDS
    growth = dataset_download_retry.RETRY_DELAY_GROWTH_FACTOR
    assert outcome.waits == [
        initial * growth**retry for retry in range(MAXIMUM_LOAD_ATTEMPTS - 1)
    ]
    assert not outcome.destination.exists()


def test_each_wait_is_jittered_between_half_and_all_of_its_ceiling():
    recorded = _RecordedTiming()
    attempts = []

    def load():
        attempts.append(1)
        if len(attempts) < MAXIMUM_LOAD_ATTEMPTS:
            msg = "connection reset"
            raise requests.ConnectionError(msg)
        return "loaded"

    assert (
        load_with_download_retry(
            load, dataset_name="uk_local_areas", timing=recorded.timing()
        )
        == "loaded"
    )
    assert recorded.delay_ranges == [
        (ceiling / 2, ceiling) for ceiling in recorded.waits
    ]


def test_the_main_path_dataset_load_retries_a_transient_download_failure(
    monkeypatch,
):
    loaded = object()
    load_calls = []

    def ensure_datasets(**options):
        load_calls.append(options)
        if len(load_calls) == 1:
            msg = "Read timed out."
            raise requests.ConnectionError(msg)
        return {"uk_local_areas_2026": loaded}

    recorded = _RecordedTiming()
    monkeypatch.setattr(
        dataset_download_retry, "DEFAULT_RETRY_TIMING", recorded.timing()
    )
    selection = DatasetSelection(
        "uk_local_areas", "hf://policyengine/data/local_areas.h5", is_default=False
    )

    dataset = _load_dataset_retrying_downloads(
        {"country": "uk", "time_period": 2026},
        selection=selection,
        country_module=SimpleNamespace(ensure_datasets=ensure_datasets),
        data_folder="/tmp/alternate",  # never written: ensure_datasets is fake
    )

    assert dataset is loaded
    assert len(load_calls) == 2  # one failed attempt, one success
    assert load_calls[0] == load_calls[1]
    assert recorded.waits == [dataset_download_retry.INITIAL_RETRY_DELAY_SECONDS]


class _StoppedAfterLoadingError(Exception):
    """Raised by a stubbed simulation build once the dataset has loaded."""


def test_the_uk_local_area_breakdown_retries_its_dataset_load(monkeypatch):
    from policyengine_simulation_executor import (
        simulation_runtime,
        uk_local_authority_metadata,
    )
    from policyengine_simulation_executor.simulation_runtime import (
        RegionResolution,
        _uk_area_simulations,
    )
    from test_simulation_output_fakes import TrackingRuntime

    recorded = _RecordedTiming()
    monkeypatch.setattr(
        dataset_download_retry, "DEFAULT_RETRY_TIMING", recorded.timing()
    )
    selection = DatasetSelection(
        "uk_local_areas", "hf://policyengine/data/local_areas.h5", is_default=False
    )
    load_calls = []

    def flaky_load_dataset(_params, *, selection, country_module, data_folder=None):
        load_calls.append((selection, country_module, data_folder))
        if len(load_calls) == 1:
            msg = "Read timed out."
            raise requests.ConnectionError(msg)
        return object()

    def stop_after_loading(*_args, **_kwargs):
        raise _StoppedAfterLoadingError

    monkeypatch.setattr(
        simulation_runtime,
        "_resolve_dataset_selection",
        lambda _params, *, region_resolution: selection,
    )
    monkeypatch.setattr(simulation_runtime, "_load_dataset", flaky_load_dataset)
    monkeypatch.setattr(
        uk_local_authority_metadata,
        "detect_uk_area_dataset_metadata",
        lambda _dataset, *, dataset_name: None,
    )
    monkeypatch.setattr(simulation_runtime, "_build_simulation", stop_after_loading)

    with pytest.raises(_StoppedAfterLoadingError):
        _uk_area_simulations(
            {"country": "uk", "time_period": 2026},
            runtime=TrackingRuntime(),
            country_module=SimpleNamespace(),
            region=RegionResolution(code="uk"),
            policies=(None, None),
        )

    assert len(load_calls) == 2  # one failed attempt, one success
    assert load_calls[0] == load_calls[1]
    assert len(recorded.waits) == 1


def test_the_annual_runtime_retries_its_dataset_load(monkeypatch):
    from policyengine_simulation_executor import simulation_runtime, state_datasets
    from policyengine_simulation_executor.simulation_runtime import (
        RegionResolution,
        _run_simulation_impl_core,
    )
    from test_simulation_output_fakes import TrackingRuntime

    recorded = _RecordedTiming()
    monkeypatch.setattr(
        dataset_download_retry, "DEFAULT_RETRY_TIMING", recorded.timing()
    )
    selection = DatasetSelection(
        "populace_us_2024_acs_local",
        "hf://policyengine/populace-us/populace_us_2024_acs_local.h5@rev",
        is_default=False,
    )
    load_calls = []

    def flaky_load_dataset(_params, *, country_module, selection, data_folder=None):
        load_calls.append((selection, data_folder))
        if len(load_calls) == 1:
            msg = "Read timed out."
            raise requests.ConnectionError(msg)
        return object()

    def stop_after_loading(*_args, **_kwargs):
        raise _StoppedAfterLoadingError

    monkeypatch.setattr(
        simulation_runtime,
        "_country_module",
        lambda _country: SimpleNamespace(model=SimpleNamespace(version="2.2.1")),
    )
    monkeypatch.setattr(
        simulation_runtime,
        "resolve_region",
        lambda **_kwargs: RegionResolution(
            code="state/ut", dataset_reference="ref", state_code="UT"
        ),
    )
    monkeypatch.setattr(
        simulation_runtime,
        "_resolve_dataset_selection",
        lambda _params, *, region_resolution: selection,
    )
    monkeypatch.setattr(simulation_runtime, "_load_dataset", flaky_load_dataset)
    monkeypatch.setattr(
        state_datasets, "baked_state_data_folder", lambda **_kwargs: None
    )
    monkeypatch.setattr(simulation_runtime, "_build_simulation", stop_after_loading)

    with pytest.raises(_StoppedAfterLoadingError):
        _run_simulation_impl_core(
            {
                "country": "us",
                "region": "state/ut",
                "time_period": 2026,
                "baseline": {},
                "reform": {},
            },
            runtime=TrackingRuntime(),
        )

    assert len(load_calls) == 2
    assert load_calls[0] == load_calls[1]
    assert len(recorded.waits) == 1


def test_the_modal_images_install_requests_for_the_retry():
    # The images install only this group, and the retry imports requests.
    project = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(
            encoding="utf-8"
        )
    )

    assert "requests>=2.32,<3" in project["dependency-groups"]["modal-simulation-image"]


def test_policyengine_passes_a_download_error_to_the_retry_unwrapped(
    monkeypatch, tmp_path
):
    """Drive the real UK ensure_datasets: its download error must reach the retry.

    Every request fails with the read timeout of release run 42040665530,
    so all three attempts reach Hugging Face before the error propagates. A
    policyengine.py that wrapped the error would stop the retry at once.
    """
    from policyengine_simulation_executor.release_bundle import (
        get_country_release_bundle,
    )

    requested = []

    def timed_out(url, **_kwargs):
        requested.append(url)
        msg = "HTTPSConnectionPool(host='us.aws.cdn.hf.co', port=443): Read timed out."
        raise requests.ConnectionError(msg)

    monkeypatch.setattr(dataset_materialization.requests, "get", timed_out)
    monkeypatch.setattr(
        dataset_download_retry, "DEFAULT_RETRY_TIMING", _RecordedTiming().timing()
    )
    bundle = get_country_release_bundle("uk")
    name = next(name for name in bundle.dataset_uris if name != bundle.default_dataset)
    selection = DatasetSelection(name, bundle.dataset_uris[name], is_default=False)

    with pytest.raises(requests.ConnectionError, match="Read timed out"):
        _load_dataset_retrying_downloads(
            {"country": "uk", "time_period": 2026},
            selection=selection,
            data_folder=str(tmp_path),
        )

    assert len(requested) == MAXIMUM_LOAD_ATTEMPTS
    assert len(set(requested)) == 1
    assert sorted(path.name for path in tmp_path.rglob("*") if path.is_file()) == []
