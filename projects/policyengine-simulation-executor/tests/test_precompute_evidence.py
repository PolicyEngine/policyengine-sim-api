"""Evidence retrieval must not replace the original calculation failure."""

from types import SimpleNamespace

import pytest

from policyengine_simulation_executor.precompute_benchmark import modal_helpers


class RetrievalError(Exception):
    """Explicit fake of the SDK's transport-error base class."""


@pytest.fixture(autouse=True)
def fake_sdk_exception(monkeypatch):
    # pytest's src/modal package shadows the SDK. This seam is mocked; no
    # image, named resource, or authenticated SDK lookup belongs in unit tests.
    monkeypatch.setattr(
        modal_helpers.modal,
        "exception",
        SimpleNamespace(Error=RetrievalError),
        raising=False,
    )


def test_failed_evidence_download_preserves_original_error(tmp_path, monkeypatch):
    def unavailable(volume, destination):
        raise RetrievalError("fixture stream timeout")

    monkeypatch.setattr(modal_helpers, "download_evidence", unavailable)
    volume = object()  # Retrieval is entirely replaced by the explicit mock.
    with pytest.raises(ValueError, match="fixture invalid input"):
        with modal_helpers.preserve_evidence(volume, tmp_path):
            raise ValueError("fixture invalid input")
    assert "fixture invalid input" in (tmp_path / "execution-failure.json").read_text()
    assert (
        "fixture stream timeout"
        in (tmp_path / "evidence-download-failure.json").read_text()
    )


def test_success_requires_successful_evidence_download(tmp_path, monkeypatch):
    def unavailable(volume, destination):
        raise RetrievalError("fixture stream timeout")

    monkeypatch.setattr(modal_helpers, "download_evidence", unavailable)
    volume = object()
    with pytest.raises(RetrievalError, match="fixture stream timeout"):
        with modal_helpers.preserve_evidence(volume, tmp_path):
            pass


def test_failed_execution_still_attempts_evidence_download(tmp_path, monkeypatch):
    downloaded = []
    monkeypatch.setattr(
        modal_helpers,
        "download_evidence",
        lambda volume, destination: downloaded.append(destination),
    )
    volume = object()
    with pytest.raises(ValueError, match="fixture invalid input"):
        with modal_helpers.preserve_evidence(volume, tmp_path):
            raise ValueError("fixture invalid input")
    assert downloaded == [tmp_path]
