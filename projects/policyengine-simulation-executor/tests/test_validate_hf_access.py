"""Tests for CI validation of local and deployed Hugging Face credentials."""

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


def test_deployed_validation_invokes_the_named_function(monkeypatch) -> None:
    expected = _audit()
    observed: dict[str, str | int] = {}

    class FunctionCall:
        def get(self, *, timeout: int):
            observed["timeout"] = timeout
            return expected.model_dump(mode="json")

    class FunctionHandle:
        @staticmethod
        def spawn() -> FunctionCall:
            return FunctionCall()

    class FunctionFactory:
        @staticmethod
        def from_name(
            application_name: str,
            function_name: str,
            *,
            environment_name: str,
        ) -> FunctionHandle:
            observed.update(
                application_name=application_name,
                function_name=function_name,
                environment_name=environment_name,
            )
            return FunctionHandle()

    monkeypatch.setattr(
        validate_hf_access.modal,
        "Function",
        FunctionFactory,
        raising=False,
    )

    result = validate_hf_access.validate_deployed_access(
        application_name="policyengine-simulation-py6-2-1",
        environment="staging",
    )

    assert result == expected
    assert observed == {
        "application_name": "policyengine-simulation-py6-2-1",
        "function_name": "verify_uk_private_hf_access",
        "environment_name": "staging",
        "timeout": 180,
    }


def test_cli_prints_only_the_safe_audit(monkeypatch, capsys) -> None:
    expected = _audit()
    monkeypatch.setattr(validate_hf_access, "validate_local_access", lambda: expected)
    monkeypatch.setattr("sys.argv", ["validate_hf_access"])

    validate_hf_access.main()

    assert json.loads(capsys.readouterr().out) == expected.model_dump(mode="json")
