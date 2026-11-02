"""Tests for GCP credentials setup in the executor's ``simulation_runtime``."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from policyengine_simulation_executor.gcp_credentials import (
    _normalize_credentials_blob,
)
from policyengine_simulation_executor.simulation_runtime import setup_gcp_credentials


def test_setup_gcp_credentials_deletes_temp_file_on_exit(
    monkeypatch, observability_runtime
):
    creds = json.dumps({"type": "service_account", "project_id": "p"})
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS_JSON", creds)
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)

    with setup_gcp_credentials(observability_runtime):
        path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        assert path is not None
        assert Path(path).exists()
        with Path(path).open() as file:
            assert json.loads(file.read())["project_id"] == "p"

    # Temp file must be gone after the context exits, and the env var
    # must be cleared so a retry doesn't chase a missing path.
    assert not Path(path).exists()
    assert os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") is None


def test_setup_gcp_credentials_cleans_up_on_exception(
    monkeypatch, observability_runtime
):
    creds = json.dumps({"type": "service_account", "project_id": "q"})
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS_JSON", creds)
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)

    captured_path: list[str] = []

    def fail_inside_credentials_context() -> None:
        with setup_gcp_credentials(observability_runtime):
            captured_path.append(os.environ["GOOGLE_APPLICATION_CREDENTIALS"])
            msg = "boom"
            raise RuntimeError(msg)

    with pytest.raises(RuntimeError, match="boom"):
        fail_inside_credentials_context()

    assert captured_path
    assert not Path(captured_path[0]).exists()


def test_setup_gcp_credentials_preserves_existing_env(
    monkeypatch, tmp_path, observability_runtime
):
    existing = tmp_path / "existing.json"
    existing.write_text("{}")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(existing))

    with setup_gcp_credentials(observability_runtime):
        assert os.environ["GOOGLE_APPLICATION_CREDENTIALS"] == str(existing)

    # Pre-existing var should not be disturbed.
    assert os.environ["GOOGLE_APPLICATION_CREDENTIALS"] == str(existing)


def test_normalize_credentials_blob_accepts_plain_json():
    payload = '{"a": 1}'
    assert _normalize_credentials_blob(payload) == payload


def test_normalize_credentials_blob_unwraps_double_escaped():
    inner = '{"a": 1}'
    wrapped = '"' + inner.replace('"', '\\"') + '"'
    # ``_normalize_credentials_blob`` expects the outer quotes to be absent
    # from the raw env var value; they're added by ``json.loads`` in tests.
    result = _normalize_credentials_blob(wrapped[1:-1])
    assert result == inner


def test_normalize_credentials_blob_reraises_on_unparseable():
    with pytest.raises(json.JSONDecodeError):
        _normalize_credentials_blob("not even close to json")
