"""Print policyengine.py bundle versions for deployment scripts."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from policyengine_simulation_executor.release_bundle import (
    get_bundled_package_version,
    get_country_release_bundle,
)


def _bundle_outputs() -> dict[str, str]:
    us_bundle = get_country_release_bundle("us")
    uk_bundle = get_country_release_bundle("uk")

    return {
        "policyengine_version": get_bundled_package_version("policyengine"),
        "policyengine_core_version": get_bundled_package_version("policyengine-core"),
        "spm_calculator_version": get_bundled_package_version("spm-calculator"),
        "us_version": get_bundled_package_version("policyengine-us"),
        "us_data_version": us_bundle.data_version,
        "uk_version": get_bundled_package_version("policyengine-uk"),
        "uk_data_version": uk_bundle.data_version,
        "us_dataset_sha256": getattr(us_bundle, "default_dataset_sha256", None) or "",
        "uk_dataset_sha256": getattr(uk_bundle, "default_dataset_sha256", None) or "",
    }


def main() -> None:
    """Report the installed bundle's package and data versions.

    ``--shell`` prints only ``key=value`` lines. Otherwise the pairs are
    appended to ``$GITHUB_OUTPUT`` when it is set and a summary line is printed.
    """
    outputs = _bundle_outputs()

    if "--shell" in sys.argv[1:]:
        for key, value in outputs.items():
            print(f"{key}={value}")  # noqa: T201  # command-line script: deployment scripts read stdout
        return

    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        output_path = Path(github_output)
        with output_path.open("a", encoding="utf-8") as file:
            for key, value in outputs.items():
                file.write(f"{key}={value}\n")

    print(  # noqa: T201  # command-line script: deployment scripts read stdout
        "Deploying with policyengine.py bundle "
        f"{outputs['policyengine_version']}: "
        f"policyengine-core={outputs['policyengine_core_version']}, "
        f"spm-calculator={outputs['spm_calculator_version']}, "
        f"policyengine-us={outputs['us_version']}, "
        f"us-data-release={outputs['us_data_version']}, "
        f"policyengine-uk={outputs['uk_version']}, "
        f"uk-data-release={outputs['uk_data_version']}, "
        f"uk-dataset-sha256={outputs['uk_dataset_sha256'] or 'unrecorded'}"
    )


if __name__ == "__main__":
    main()
