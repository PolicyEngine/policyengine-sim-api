"""Print policyengine.py bundle versions for deployment scripts."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from src.modal.dependency_pins import project_dependency_pin
from policyengine_simulation_executor.release_bundle import get_country_release_bundle


def _bundle_outputs() -> dict[str, str]:
    us_bundle = get_country_release_bundle("us")
    uk_bundle = get_country_release_bundle("uk")

    return {
        "policyengine_version": us_bundle.policyengine_version,
        "policyengine_core_version": project_dependency_pin("policyengine-core"),
        "us_version": us_bundle.model_version,
        "us_data_version": us_bundle.data_version,
        "uk_version": uk_bundle.model_version,
        "uk_data_version": uk_bundle.data_version,
    }


def main() -> None:
    outputs = _bundle_outputs()

    if "--shell" in sys.argv[1:]:
        for key, value in outputs.items():
            print(f"{key}={value}")
        return

    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        output_path = Path(github_output)
        with output_path.open("a", encoding="utf-8") as file:
            for key, value in outputs.items():
                file.write(f"{key}={value}\n")

    print(
        "Deploying with policyengine.py bundle "
        f"{outputs['policyengine_version']}: "
        f"policyengine-core={outputs['policyengine_core_version']}, "
        f"policyengine-us={outputs['us_version']}, "
        f"us-data-release={outputs['us_data_version']}, "
        f"policyengine-uk={outputs['uk_version']}, "
        f"uk-data-release={outputs['uk_data_version']}"
    )


if __name__ == "__main__":
    main()
