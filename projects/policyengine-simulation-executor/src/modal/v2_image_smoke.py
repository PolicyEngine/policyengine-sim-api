# pyright: reportAttributeAccessIssue=false, reportFunctionMemberAccess=false
"""Pre-merge image smoke: build every v2 Modal image and import its entry points.

The coordinator, UK worker and household images are the ones the v2
application deploys. The US worker image is built without its last layer, the
Stage 12 cache: only a deployment can add that layer, because the deployment's
precompute writes the cache and the image build reads it with the deployment's
Cloud Storage credential. The layer adds data files, not Python packages.

In each image the smoke imports the v2 application module, as Modal does
before it runs any of the application's functions, and the modules those
functions import while handling a request. In the household image it also
calculates the release validation household for each country, which shows the
image calculates households without datasets.

Usage:
    uv run modal run --env=staging src/modal/v2_image_smoke.py
"""

from src.modal.v2_app import (
    build_v2_image,
    coordinator_image,
    household_image,
    uk_worker_image,
)

import modal

SMOKE_APP_NAME = "policyengine-simulation-v2-image-smoke"
app = modal.App(SMOKE_APP_NAME)

us_worker_image_without_cache = build_v2_image(("us",))

# Modules the report coordinator, simulation workers and validation functions
# import when they start or while they handle a request.
REPORT_ENTRY_POINT_MODULES = (
    "src.modal.v2_app",
    "policyengine_simulation_executor.stage12_runtime",
    "policyengine_simulation_executor.stage12_worker_validation",
    "policyengine_simulation_executor.simulation_runtime",
    "policyengine_simulation_executor.spm",
    "policyengine_simulation_executor.uk_local_authority_metadata",
    "microdf",
    "policyengine.tax_benefit_models.us.datasets",
    "policyengine.tax_benefit_models.uk.datasets",
)
# Modules the household functions import when they start or calculate.
HOUSEHOLD_ENTRY_POINT_MODULES = (
    "src.modal.v2_app",
    "policyengine_simulation_executor.household_calculation",
    "policyengine_simulation_executor.household_validation",
)


def _import_modules(names: tuple[str, ...]) -> list[str]:
    """Import each named module, raising on the first that fails."""
    import importlib  # noqa: PLC0415  # smoke app: standard-library import kept with the in-image check that uses it

    for name in names:
        importlib.import_module(name)
    return list(names)


def _report_image_smoke() -> dict[str, object]:
    """Import the report entry points; return them and the installed bundle version."""
    from policyengine_simulation_executor.stage12_bundle import (  # noqa: PLC0415  # smoke check: imported inside the Modal image, which is what this verifies
        load_stage12_bundle,
    )

    return {
        "modules": _import_modules(REPORT_ENTRY_POINT_MODULES),
        "policyengine_version": load_stage12_bundle().bundle.policyengine_version,
    }


@app.function(image=coordinator_image, timeout=600, memory=8192)
def smoke_v2_coordinator_image() -> dict[str, object]:
    """Import the v2 entry points inside the report coordinator's image."""
    return _report_image_smoke()


@app.function(image=uk_worker_image, timeout=600, memory=8192)
def smoke_v2_uk_worker_image() -> dict[str, object]:
    """Import the v2 entry points inside the UK simulation worker's image."""
    return _report_image_smoke()


@app.function(image=us_worker_image_without_cache, timeout=600, memory=8192)
def smoke_v2_us_worker_image() -> dict[str, object]:
    """Import the v2 entry points inside the US simulation worker's image.

    The image has every layer of the deployed image except the Stage 12 cache.
    """
    return _report_image_smoke()


@app.function(image=household_image, timeout=900, memory=(4096, 16384))
def smoke_v2_household_image() -> dict[str, object]:
    """Import the household entry points and calculate each validation household.

    Raises when a calculation fails or does not return a household net income.
    """
    from datetime import (  # noqa: PLC0415  # smoke app: standard-library import kept with the in-image check that uses it
        UTC,
        datetime,
    )

    from policyengine_simulation_contract.household_execution import (  # noqa: PLC0415  # smoke check: imported inside the Modal image, which is what this verifies
        HouseholdWorkerIdentity,
        household_callable_name,
    )
    from policyengine_simulation_executor import (  # noqa: PLC0415  # smoke check: imported inside the Modal image, which is what this verifies
        household_calculation,
    )
    from policyengine_simulation_executor.household_validation import (  # noqa: PLC0415  # smoke check: imported inside the Modal image, which is what this verifies
        check_household_validation,
        household_validation_request,
    )
    from policyengine_simulation_executor.stage12_bundle import (  # noqa: PLC0415  # smoke check: imported inside the Modal image, which is what this verifies
        load_stage12_bundle,
    )

    modules = _import_modules(HOUSEHOLD_ENTRY_POINT_MODULES)
    year = datetime.now(UTC).year
    net_incomes: dict[str, float] = {}
    for country_bundle in load_stage12_bundle().bundle.countries:
        country = country_bundle.country
        request = household_validation_request(country, year=year)
        worker = HouseholdWorkerIdentity(
            application_name=SMOKE_APP_NAME,
            function_name=household_callable_name(country),
        )
        outcome = household_calculation.calculate_household(
            request, worker=worker, required_country=country
        )
        net_incomes[country] = check_household_validation(
            outcome.model_dump(mode="json"), request=request, worker=worker
        )
    return {"modules": modules, "household_net_income": net_incomes}


@app.local_entrypoint()
def main() -> None:
    """Run every v2 image smoke remotely and print their reports."""
    reports = {
        "coordinator": smoke_v2_coordinator_image.remote(),
        "uk_worker": smoke_v2_uk_worker_image.remote(),
        "us_worker": smoke_v2_us_worker_image.remote(),
        "household": smoke_v2_household_image.remote(),
    }
    print(reports)  # noqa: T201  # Modal local entrypoint: runs locally, and stdout is the smoke's report
    print("v2 image smoke OK")  # noqa: T201  # Modal local entrypoint: runs locally, and stdout is the smoke's report
