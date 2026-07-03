"""Pre-merge image smoke: import the gateway inside its real image.

Runs the true entrypoint surface — every package module, the explicit
``import logfire`` (issue #602's crash path), and the real ASGI factory —
inside the exact image the deploy ships. Catches in-image breakage
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
    import importlib
    import os
    import pkgutil

    # The #602 crash path: logfire imports importlib_metadata at import
    # time; the app only touches logfire lazily, so import it explicitly.
    import logfire  # noqa: F401

    import policyengine_simulation_contract
    import policyengine_simulation_gateway
    import policyengine_simulation_observability

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
    from policyengine_simulation_gateway.testing import create_gateway_app

    asgi_app = create_gateway_app()
    return {
        "modules_imported": len(imported),
        "routes": sorted(
            f"{sorted(route.methods)[0]} {route.path}"
            for route in asgi_app.routes
            if hasattr(route, "methods")
        ),
    }


@app.local_entrypoint()
def main():
    report = smoke_import_gateway.remote()
    print(report)
    assert report["modules_imported"] > 0
    assert any("/health" in route for route in report["routes"])
    print("gateway image smoke OK")
