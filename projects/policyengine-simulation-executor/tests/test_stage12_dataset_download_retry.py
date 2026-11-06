"""Tests for Stage 12's retry of a dataset load after a transient download failure.

Stage 12 keeps its own copy of the main path's retry, so these tests check
the copy on its own.
"""

from __future__ import annotations

import tomllib
from contextlib import nullcontext
from pathlib import Path

import pytest
import requests
from policyengine.provenance.dataset_materialization import (
    DatasetMaterializationError,
)
from policyengine_simulation_executor.stage12_runtime import (
    dataset_download_retry as stage12_retry,
)
from policyengine_simulation_executor.stage12_runtime.dataset_download_retry import (
    MAXIMUM_LOAD_ATTEMPTS,
    RetryTiming,
    load_with_download_retry,
)
from stage12_runtime_support import planned_simulation


def _http_error(status_code: int) -> requests.HTTPError:
    response = requests.Response()
    response.status_code = status_code
    return requests.HTTPError(f"{status_code} error", response=response)


def _recording_timing(waits: list[float]) -> RetryTiming:
    return RetryTiming(sleep=waits.append, choose_delay=lambda _lower, upper: upper)


def _load_failing_with(errors: list[Exception]):
    """Return a load that raises ``errors`` in turn, then returns "loaded"."""
    attempts: list[int] = []

    def load() -> str:
        attempts.append(1)
        if errors:
            raise errors.pop(0)
        return "loaded"

    return load, attempts


@pytest.mark.parametrize(
    "transient_error",
    [
        requests.ConnectionError("Read timed out."),
        requests.exceptions.ReadTimeout("read timed out waiting for headers"),
        requests.exceptions.ChunkedEncodingError("cut off"),
        _http_error(500),
        _http_error(503),
    ],
    ids=["read-timeout-while-streaming", "read-timeout", "body-cut-off", "500", "503"],
)
def test_stage12_retries_a_transient_download_failure(transient_error) -> None:
    load, attempts = _load_failing_with([transient_error])
    waits: list[float] = []

    assert (
        load_with_download_retry(
            load, dataset_name="uk_local_areas", timing=_recording_timing(waits)
        )
        == "loaded"
    )
    assert len(attempts) == 2  # one failed attempt, one success
    assert waits == [stage12_retry.INITIAL_RETRY_DELAY_SECONDS]


@pytest.mark.parametrize(
    "permanent_error",
    [
        DatasetMaterializationError("Hugging Face rejected the configured credentials"),
        DatasetMaterializationError("Could not find UK dataset"),
        DatasetMaterializationError("Downloaded UK dataset has sha256 0, expected 1"),
        _http_error(401),
        _http_error(403),
        _http_error(404),
        _http_error(400),
    ],
    ids=["rejected", "missing", "sha256-mismatch", "401", "403", "404", "400"],
)
def test_stage12_never_retries_a_rejected_missing_or_corrupt_dataset(
    permanent_error,
) -> None:
    load, attempts = _load_failing_with([permanent_error])
    waits: list[float] = []

    with pytest.raises(type(permanent_error)):
        load_with_download_retry(
            load, dataset_name="uk_local_areas", timing=_recording_timing(waits)
        )
    assert len(attempts) == 1
    assert waits == []


def test_stage12_retries_stop_after_the_attempt_limit() -> None:
    errors: list[Exception] = [
        requests.ConnectionError("connection reset")
        for _ in range(MAXIMUM_LOAD_ATTEMPTS + 1)
    ]
    load, attempts = _load_failing_with(errors)
    waits: list[float] = []

    with pytest.raises(requests.ConnectionError):
        load_with_download_retry(
            load, dataset_name="uk_local_areas", timing=_recording_timing(waits)
        )
    assert len(attempts) == MAXIMUM_LOAD_ATTEMPTS
    initial = stage12_retry.INITIAL_RETRY_DELAY_SECONDS
    growth = stage12_retry.RETRY_DELAY_GROWTH_FACTOR
    assert waits == [
        initial * growth**retry for retry in range(MAXIMUM_LOAD_ATTEMPTS - 1)
    ]


class _StoppedAfterLoadingError(Exception):
    """Raised by a stubbed simulation build once the dataset has loaded."""


def test_stage12_worker_retries_a_dataset_download_that_timed_out(
    monkeypatch,
) -> None:
    from policyengine_simulation_contract.stage12_execution import SimulationRole
    from policyengine_simulation_executor import simulation_runtime
    from policyengine_simulation_executor.simulation_runtime import (
        DatasetSelection,
        RegionResolution,
    )
    from policyengine_simulation_executor.stage12_cache import state_datasets
    from policyengine_simulation_executor.stage12_runtime import simulation as worker

    waits: list[float] = []
    monkeypatch.setattr(stage12_retry, "DEFAULT_RETRY_TIMING", _recording_timing(waits))
    selection = DatasetSelection(
        name="populace_us_2024_acs_local",
        uri="hf://policyengine/populace-us/populace_us_2024_acs_local.h5@rev",
        is_default=False,
    )
    loads: list[tuple[object, object]] = []

    def flaky_load_dataset(_params, *, selection, country_module, data_folder=None):
        del country_module
        loads.append((selection, data_folder))
        if len(loads) == 1:
            msg = "Read timed out."
            raise requests.ConnectionError(msg)
        return object()

    def stop_after_loading(*_args, **_kwargs):
        raise _StoppedAfterLoadingError

    monkeypatch.setattr(worker, "_require_installed_bundle", lambda _: None)
    monkeypatch.setattr(simulation_runtime, "setup_gcp_credentials", nullcontext)
    monkeypatch.setattr(
        simulation_runtime, "_country_module", lambda _country: object()
    )
    monkeypatch.setattr(
        simulation_runtime,
        "resolve_region",
        lambda **_: RegionResolution(code="state/ut", state_code="UT"),
    )
    monkeypatch.setattr(
        simulation_runtime,
        "_resolve_dataset_selection",
        lambda _params, *, region_resolution: selection,
    )
    monkeypatch.setattr(simulation_runtime, "_load_dataset", flaky_load_dataset)
    monkeypatch.setattr(state_datasets, "baked_state_data_folder", lambda **_: None)
    monkeypatch.setattr(
        worker, "detect_uk_run_dataset_metadata", lambda *_a, **_k: None
    )
    monkeypatch.setattr(worker, "build_stage12_simulation", stop_after_loading)

    with pytest.raises(_StoppedAfterLoadingError):
        worker.calculate_simulation_frames(planned_simulation(SimulationRole.BASELINE))

    assert len(loads) == 2  # one failed attempt, one success
    assert loads[0] == loads[1]
    assert len(waits) == 1


def test_the_stage12_images_install_requests_for_the_retry() -> None:
    # The Stage 12 images install only this group, and the retry imports
    # requests.
    project = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(
            encoding="utf-8"
        )
    )

    assert "requests>=2.32,<3" in project["dependency-groups"]["modal-simulation-image"]
