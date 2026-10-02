"""Validate UK private-data access in CI or a deployed Modal application."""

from __future__ import annotations

import argparse

import modal

from policyengine_simulation_executor.hf_access_validation import (
    HFCredentialAudit,
    validate_configured_uk_private_hf_access,
)

DEPLOYED_VALIDATION_FUNCTION = "verify_uk_private_hf_access"
DEPLOYED_VALIDATION_WAIT_SECONDS = 180


def validate_local_access() -> HFCredentialAudit:
    """Validate the credential supplied directly to the CI process."""

    return validate_configured_uk_private_hf_access()


def validate_deployed_access(
    *,
    application_name: str,
    environment: str,
) -> HFCredentialAudit:
    """Validate the credential injected into an actual deployed application."""

    function = modal.Function.from_name(
        application_name,
        DEPLOYED_VALIDATION_FUNCTION,
        environment_name=environment,
    )
    call = function.spawn()
    return HFCredentialAudit.model_validate(
        call.get(timeout=DEPLOYED_VALIDATION_WAIT_SECONDS)
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate UK private-data Hugging Face access",
    )
    parser.add_argument("--application-name")
    parser.add_argument("--environment")
    args = parser.parse_args()
    if bool(args.application_name) != bool(args.environment):
        parser.error("--application-name and --environment must be supplied together")

    audit = (
        validate_deployed_access(
            application_name=args.application_name,
            environment=args.environment,
        )
        if args.application_name
        else validate_local_access()
    )
    print(audit.model_dump_json())


if __name__ == "__main__":
    main()
