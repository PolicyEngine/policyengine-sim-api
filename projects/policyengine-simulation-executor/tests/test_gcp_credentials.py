"""Tests for GCP credentials setup in the executor's ``simulation_runtime``."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from environment_reads import EnvironmentReadRecorder
from policyengine_simulation_executor import gcp_credentials
from policyengine_simulation_executor.gcp_credentials import (
    _normalize_credentials_blob,
    skip_gcp_credential_setup,
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


def test_skipped_credential_setup_reads_no_variable_and_writes_no_file(
    monkeypatch, observability_runtime
):
    # The v2 snapshot warm-up calculates from the image's files only, and
    # nothing before the snapshot may read a Secret.
    monkeypatch.setenv(
        "GOOGLE_APPLICATION_CREDENTIALS_JSON", json.dumps({"type": "service_account"})
    )
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)

    def refuse_temporary_file(*_args, **_kwargs):
        msg = "a skipped credential setup must write no credentials file"
        raise AssertionError(msg)

    monkeypatch.setattr(
        gcp_credentials.tempfile, "NamedTemporaryFile", refuse_temporary_file
    )
    environment = EnvironmentReadRecorder(os.environ)
    monkeypatch.setattr(os, "environ", environment)

    with skip_gcp_credential_setup(), setup_gcp_credentials(observability_runtime):
        names_read_inside = set(environment.names_read)
        credentials_path_inside = environment.environment.get(
            "GOOGLE_APPLICATION_CREDENTIALS"
        )

    assert names_read_inside == set()
    assert credentials_path_inside is None
    assert environment.names_read == set()


def test_credential_setup_writes_credentials_again_after_a_skipped_block(
    monkeypatch, observability_runtime
):
    monkeypatch.setenv(
        "GOOGLE_APPLICATION_CREDENTIALS_JSON", json.dumps({"type": "service_account"})
    )
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)

    def fail_inside_skipped_block() -> None:
        with skip_gcp_credential_setup():
            msg = "warm-up failed"
            raise RuntimeError(msg)

    with pytest.raises(RuntimeError, match="warm-up failed"):
        fail_inside_skipped_block()

    with setup_gcp_credentials(observability_runtime):
        assert os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") is not None
