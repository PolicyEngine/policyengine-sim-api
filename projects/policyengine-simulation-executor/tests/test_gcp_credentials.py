"""Tests for GCP credentials setup in ``policyengine_simulation_executor.simulation_runtime``."""

from __future__ import annotations

import json
import os

import pytest

from policyengine_simulation_executor.simulation_runtime import (
    _normalize_credentials_blob,
    setup_gcp_credentials,
)


def test_setup_gcp_credentials_deletes_temp_file_on_exit(monkeypatch):
    creds = json.dumps({"type": "service_account", "project_id": "p"})
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS_JSON", creds)
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)

    with setup_gcp_credentials():
        path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        assert path is not None
        assert os.path.exists(path)
        with open(path) as file:
            assert json.loads(file.read())["project_id"] == "p"

    # Temp file must be gone after the context exits, and the env var
    # must be cleared so a retry doesn't chase a missing path.
    assert not os.path.exists(path)
    assert os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") is None


def test_setup_gcp_credentials_cleans_up_on_exception(monkeypatch):
    creds = json.dumps({"type": "service_account", "project_id": "q"})
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS_JSON", creds)
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)

    captured_path: list[str] = []
    with pytest.raises(RuntimeError):
        with setup_gcp_credentials():
            captured_path.append(os.environ["GOOGLE_APPLICATION_CREDENTIALS"])
            raise RuntimeError("boom")

    assert captured_path
    assert not os.path.exists(captured_path[0])


def test_setup_gcp_credentials_preserves_existing_env(monkeypatch, tmp_path):
    existing = tmp_path / "existing.json"
    existing.write_text("{}")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(existing))

    with setup_gcp_credentials():
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
