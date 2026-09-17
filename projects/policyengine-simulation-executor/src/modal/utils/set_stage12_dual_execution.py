"""Atomically enable or disable Stage 12 parallel comparison execution."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone

import modal

from policyengine_simulation_contract.stage12_control import (
    Stage12DualExecutionControl,
    V2_DUAL_EXECUTION_CONTROL_NAME,
    publish_control,
)


def set_dual_execution(
    *,
    modal_environment: str,
    deployment_environment: str,
    control_name: str,
    manifest_selection_enabled: bool,
    economy_enabled: bool,
) -> None:
    if deployment_environment not in {"staging", "production"}:
        raise ValueError("deployment environment must be staging or production")
    store = modal.Dict.from_name(
        control_name,
        environment_name=modal_environment,
        create_if_missing=True,
    )
    publish_control(
        store,
        Stage12DualExecutionControl(
            environment=deployment_environment,
            manifest_selection_enabled=manifest_selection_enabled,
            economy_enabled=economy_enabled,
            updated_at=datetime.now(timezone.utc),
        ),
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--modal-environment", required=True)
    parser.add_argument(
        "--deployment-environment",
        required=True,
        choices=("staging", "production"),
    )
    parser.add_argument(
        "--control-name",
        default=V2_DUAL_EXECUTION_CONTROL_NAME,
    )
    parser.add_argument(
        "--manifest-selection-enabled",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    parser.add_argument(
        "--economy-enabled",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    args = parser.parse_args()
    set_dual_execution(
        modal_environment=args.modal_environment,
        deployment_environment=args.deployment_environment,
        control_name=args.control_name,
        manifest_selection_enabled=args.manifest_selection_enabled,
        economy_enabled=args.economy_enabled,
    )


if __name__ == "__main__":
    main()
