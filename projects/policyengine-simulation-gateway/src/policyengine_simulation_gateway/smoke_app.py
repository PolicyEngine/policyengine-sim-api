"""Pre-merge image smoke: import the gateway inside its real image.

Runs the true entrypoint surface — every package module and the real ASGI
factory — inside the exact image the deploy ships. Catches in-image breakage
(missing lock packages, mount gaps, import-time crashes) before merge
instead of at the post-merge beta integration tests.

Usage:
    uv run modal run --env=staging src/policyengine_simulation_gateway/smoke_app.py

The image is built by the same ``build_gateway_image()`` as the deployed
app, so layers are cache-identical: a passing smoke leaves the real
deploy a fast-forward.
"""

import modal

from policyengine_simulation_gateway.app import build_gateway_image

app = modal.App("policyengine-simulation-gateway-smoke")


@app.function(image=build_gateway_image(), timeout=300)
def smoke_import_gateway() -> dict:
    """Import every shipped package module and build the ASGI app in-image.

    Return the number of modules imported and the app's ``METHOD path`` routes.
    """
    import importlib  # noqa: PLC0415  # smoke app: standard-library import kept with the in-image check's other imports
    import os  # noqa: PLC0415  # smoke app: standard-library import kept with the in-image check's other imports
    import pkgutil  # noqa: PLC0415  # smoke app: standard-library import kept with the in-image check's other imports

    import policyengine_simulation_contract  # noqa: PLC0415  # smoke check: the import itself, inside the Modal image, is what this verifies
    import policyengine_simulation_observability  # noqa: PLC0415  # smoke check: the import itself, inside the Modal image, is what this verifies

    import policyengine_simulation_gateway  # noqa: PLC0415  # smoke check: the import itself, inside the Modal image, is what this verifies

    imported = []
    for package in (
        policyengine_simulation_gateway,
        policyengine_simulation_contract,
        policyengine_simulation_observability,
    ):
        for module in pkgutil.walk_packages(
            package.__path__, prefix=f"{package.__name__}."
        ):
            importlib.import_module(module.name)
            imported.append(module.name)

    # Build the real ASGI app. The testing factory wires the real router
    # and observability init; auth env is staged so require_auth's module
    # import surface is exercised without real JWT material.
    os.environ.setdefault("GATEWAY_AUTH_ISSUER", "https://smoke.invalid/")
    os.environ.setdefault("GATEWAY_AUTH_AUDIENCE", "smoke")
    from policyengine_simulation_gateway.testing import (  # noqa: PLC0415  # smoke check: imported after the auth environment is staged
        create_gateway_app,
    )

    asgi_app = create_gateway_app()
    return {
        "modules_imported": len(imported),
        "routes": sorted(
            f"{min(route.methods)} {route.path}"
            for route in asgi_app.routes
            if hasattr(route, "methods")
        ),
    }


@app.local_entrypoint()
def main() -> None:
    """Run the in-image smoke remotely; fail unless modules and /health loaded."""
    report = smoke_import_gateway.remote()
    print(report)  # noqa: T201  # Modal local entrypoint: runs locally, and stdout is the smoke's report
    if report["modules_imported"] <= 0:
        msg = f"Gateway image smoke imported no modules: {report!r}"
        raise RuntimeError(msg)
    if not any("/health" in route for route in report["routes"]):
        msg = f"Gateway image smoke found no /health route: {report!r}"
        raise RuntimeError(msg)
    print("gateway image smoke OK")  # noqa: T201  # Modal local entrypoint: runs locally, and stdout is the smoke's report
