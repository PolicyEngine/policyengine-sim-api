"""Validate UK private-data access directly in CI."""

from __future__ import annotations

from policyengine_simulation_executor.hf_access_validation import (
    HFCredentialAudit,
    validate_configured_uk_private_hf_access,
)


def validate_local_access() -> HFCredentialAudit:
    """Validate the credential supplied directly to the CI process."""

    return validate_configured_uk_private_hf_access()


def main() -> None:
    print(validate_local_access().model_dump_json())


if __name__ == "__main__":
    main()
