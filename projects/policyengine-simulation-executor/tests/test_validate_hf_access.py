"""Tests for direct CI validation of the Hugging Face credential."""

from __future__ import annotations

import json

from policyengine_simulation_executor.hf_access_validation import (
    HFCredentialAudit,
)
from src.modal.utils import validate_hf_access


def _audit() -> HFCredentialAudit:
    return HFCredentialAudit(
        token_fingerprint="abc123def456",
        account="policyengine-runtime-data-reader",
        token_display_name="pe-uk-private-hf-read-token",
        artifacts=(),
    )


def test_local_validation_uses_the_configured_credential(monkeypatch) -> None:
    expected = _audit()
    monkeypatch.setattr(
        validate_hf_access,
        "validate_configured_uk_private_hf_access",
        lambda: expected,
    )

    assert validate_hf_access.validate_local_access() == expected


def test_cli_prints_only_the_safe_audit(monkeypatch, capsys) -> None:
    expected = _audit()
    monkeypatch.setattr(validate_hf_access, "validate_local_access", lambda: expected)

    validate_hf_access.main()

    assert json.loads(capsys.readouterr().out) == expected.model_dump(mode="json")
